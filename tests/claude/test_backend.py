"""Fixture-driven regression tests for the Claude backend.

These tests load real `tmux capture-pane` snapshots (under `fixtures/`) and
assert that `ClaudeBackend` interprets each one correctly. They exist to
catch UI regressions: if Claude renames a menu header, changes a spinner
glyph, or restructures option rendering, the relevant test fails and points
at the specific surface that needs reparsing.
"""
from __future__ import annotations

import pytest

from kage.backends.claude import (
    AUTONOMOUS_SYSTEM_PROMPT,
    ClaudeBackend,
    DONE_RE,
    INTERACTIVE_TOOLS,
    SPINNER_RE,
    TOOL_CALL_RE,
)
from tests.conftest import load_fixture


@pytest.fixture
def backend() -> ClaudeBackend:
    return ClaudeBackend()


def fx(name: str) -> str:
    return load_fixture("claude", name)


# ---------------------------------------------------------------------------
# state classification — every fixture must classify cleanly into exactly one
# of: idle / busy / menu / done. If a UI change creates ambiguity, these tests
# surface it before kage runs into the same ambiguity in production.

@pytest.mark.parametrize(
    "fixture, expected_menu, expected_busy",
    [
        ("idle_prompt", False, False),
        ("simple_response", False, False),
        ("multi_turn_unicode", False, False),
        ("busy_spinner", False, True),
        ("plan_menu", True, False),
        ("permission_menu", True, False),
        ("askuserquestion_menu", True, False),
        ("trust_dialog", True, False),
        ("subagent_response", False, False),
        ("tool_calls_response", False, False),
        ("logged_out", False, False),
    ],
)
def test_state_classification(backend, fixture, expected_menu, expected_busy):
    pane = fx(fixture)
    assert backend.is_menu(pane) is expected_menu, "is_menu wrong"
    assert backend.is_busy(pane) is expected_busy, "is_busy wrong"


# ---------------------------------------------------------------------------
# ready_marker — kage uses this during `start()` to decide claude has booted.

@pytest.mark.parametrize(
    "fixture, expected_ready",
    [
        ("idle_prompt", True),
        ("simple_response", True),
        ("busy_spinner", True),
        # trust_dialog has no idle `❯` prompt yet — start() instead accepts
        # `is_menu` as the ready signal for this case.
        ("trust_dialog", False),
    ],
)
def test_ready_marker(backend, fixture, expected_ready):
    assert backend.ready_marker(fx(fixture)) is expected_ready


# ---------------------------------------------------------------------------
# done-marker counting — kage uses the count delta to detect new responses.
# Unicode verbs (Sautéed, Pondéring) must count; this is the regression that
# broke multi-turn sessions before the `\S+` fix.

@pytest.mark.parametrize(
    "fixture, expected_count",
    [
        ("idle_prompt", 0),
        ("simple_response", 1),
        ("multi_turn_unicode", 2),
        ("busy_spinner", 0),
        ("subagent_response", 1),
        ("tool_calls_response", 1),
        ("plan_menu", 0),  # plan mode never emits the done marker
        ("permission_menu", 0),  # menu present, response not done
        ("logged_out", 1),
    ],
)
def test_done_marker_count(backend, fixture, expected_count):
    assert backend.done_marker_count(fx(fixture)) == expected_count


# ---------------------------------------------------------------------------
# response extraction — the text the model produced, stripped of tool chrome.

def test_extract_response_simple(backend):
    assert backend.extract_response(fx("simple_response")) == "4"


def test_extract_response_multi_turn_unicode(backend):
    # Only the most recent turn's response is returned.
    assert backend.extract_response(fx("multi_turn_unicode")) == "4242"


def test_extract_response_subagent_no_tool_chrome(backend):
    """Sub-agent tool name with a space must not bleed into the response."""
    text = backend.extract_response(fx("subagent_response"))
    assert "Research Agent" not in text, "tool-call line leaked into response"
    assert "影 (kage)" in text


def test_extract_response_tool_calls_stripped(backend):
    text = backend.extract_response(fx("tool_calls_response"))
    assert text.strip() == "done"
    assert "Bash" not in text


def test_extract_response_idle_is_empty(backend):
    assert backend.extract_response(fx("idle_prompt")) == ""


def test_logged_out_error(backend):
    error = backend.extract_error(fx("logged_out"))
    assert error is not None
    assert error.reason == "not_logged_in"
    assert "Not logged in" in error.message
    assert "security unlock-keychain" in error.message


def test_prior_logged_out_error_does_not_poison_new_turn(backend):
    pane = fx("logged_out") + "\n❯ say ok\n⏺ ok\n✻ Responded for 1s\n❯"
    assert backend.extract_error(pane) is None


# ---------------------------------------------------------------------------
# menu extraction — question must be the actual question, options must be
# only the real options (not numbered list items from surrounding content).

def test_plan_menu_question_and_options(backend):
    menu = backend.extract_menu(fx("plan_menu"))
    assert menu is not None
    assert menu.question == "Would you like to proceed?"
    assert menu.options == [
        "Yes, and bypass permissions",
        "Yes, manually approve edits",
        "No, refine with Ultraplan on Claude Code on the web",
        "Tell Claude what to change",
    ]


def test_permission_menu_dynamic_question(backend):
    menu = backend.extract_menu(fx("permission_menu"))
    assert menu is not None
    assert menu.question == "Do you want to create hello.txt?"
    assert menu.options == [
        "Yes",
        "Yes, allow all edits during this session (shift+tab)",
        "No",
    ]


def test_askuserquestion_menu(backend):
    menu = backend.extract_menu(fx("askuserquestion_menu"))
    assert menu is not None
    assert menu.question == "What kind of thing are you trying to deploy?"
    assert menu.options == [
        "Web app / frontend",
        "Backend / API",
        "Script / cron job",
        "Something else",
        "Type something.",
    ]


def test_trust_dialog(backend):
    menu = backend.extract_menu(fx("trust_dialog"))
    assert menu is not None
    assert "trust" in menu.question.lower()
    assert menu.options == ["Yes, I trust this folder", "No, exit"]


def test_no_menu_when_idle(backend):
    assert backend.extract_menu(fx("idle_prompt")) is None
    assert backend.extract_menu(fx("simple_response")) is None


def test_start_command_removes_tui_interaction_tools_by_default(backend):
    cmd = backend.start_command(system_prompt="custom rule")

    assert "--disallowed-tools=" + ",".join(INTERACTIVE_TOOLS) in cmd
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "custom rule" in prompt
    assert AUTONOMOUS_SYSTEM_PROMPT in prompt


# ---------------------------------------------------------------------------
# regex sanity — direct unit tests on the patterns. Faster to debug than the
# fixture tests when a regex change accidentally narrows coverage.

@pytest.mark.parametrize(
    "line, expected",
    [
        ("✻ Cogitated for 10s", True),
        ("✻ Sautéed for 2s", True),     # Unicode verb (regression for [A-Za-z]+ bug)
        ("✻ Churned for 30s", True),
        ("✻ Pondéring for 1s", True),    # Unicode in middle of verb
        ("✻ Puttered for 1m 16s", True),  # >60s turn: compound m/s (regression)
        ("✻ Cogitated for 2m", True),     # whole-minute, no seconds
        ("✻ Churned for 1h 2m 3s", True), # multi-hour turn
        ("· Percolating…", False),        # spinner, not done
        ("⏺ Bash(ls)", False),
        ("", False),
    ],
)
def test_done_regex(line, expected):
    assert bool(DONE_RE.match(line)) is expected


@pytest.mark.parametrize(
    "line, expected",
    [
        ("· Percolating…", True),
        ("✻ Puttering… (21s)", True),
        ("✢ Percolating… (1m 16s)", True),
        ("· Precipitating… (5s · ↓ 102 tokens)", True),
        ("❯ help me with something", False),  # user prompt, not a spinner
        ("✻ Cogitated for 10s", False),       # done marker, not a spinner
        ("Loading…", False),                   # no bullet prefix
        ("", False),
    ],
)
def test_spinner_regex(line, expected):
    assert bool(SPINNER_RE.search(line)) is expected


@pytest.mark.parametrize(
    "line, expected_name",
    [
        ("⏺ Bash(ls -la)", "Bash"),
        ("⏺ Read(file_path: /tmp/x)", "Read"),
        ("⏺ Research Agent(find shadow word)", "Research Agent"),
        ("⏺ Audit Agent(clean up file)", "Audit Agent"),
        ("⏺ TodoWrite(todos: [...])", "TodoWrite"),
        ("not a tool call", None),
        ("⏺ lowercase(x)", None),  # tool names always start uppercase
    ],
)
def test_tool_call_regex(line, expected_name):
    m = TOOL_CALL_RE.match(line)
    if expected_name is None:
        assert m is None
    else:
        assert m is not None
        assert m.group(1) == expected_name
