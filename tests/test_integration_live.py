"""Live end-to-end smoke tests against a real `claude` + tmux.

Skipped by default so CI stays hermetic. Run before a release with:

    KAGE_LIVE=1 python -m pytest tests/test_integration_live.py -v

Requirements: tmux installed, `claude` on PATH and logged into a subscription,
network access. Each test uses a throwaway session and cleans up after itself.
"""
import json
import os
import shutil
import subprocess
import sys
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("KAGE_LIVE"),
    reason="live integration test; set KAGE_LIVE=1 (needs claude + tmux + network)",
)

if not (shutil.which("tmux") and shutil.which("claude")):
    pytestmark = pytest.mark.skip(reason="tmux and/or claude not on PATH")

KAGE = [sys.executable, "-m", "kage"]


def run(*args, timeout=150, stdin=subprocess.DEVNULL):
    """Invoke the kage CLI; return CompletedProcess (stdin defaults to /dev/null)."""
    return subprocess.run(
        KAGE + list(args),
        capture_output=True, text=True, timeout=timeout, stdin=stdin,
    )


@pytest.fixture
def session():
    """A unique session name, removed afterwards."""
    name = "kagelive_" + uuid.uuid4().hex[:8]
    yield name
    subprocess.run(KAGE + ["session", "rm", name],
                   capture_output=True, text=True, timeout=30)


def test_doctor_ok():
    r = run("doctor", timeout=30)
    assert r.returncode == 0, r.stderr


def test_oneshot_math():
    r = run("claude", "what is 2+2? reply with only the number")
    assert r.returncode == 0, r.stderr
    assert "4" in r.stdout


def test_named_session_continuity(session):
    r1 = run("claude", "--session", session,
             "Remember the word GRAPEFRUIT. Reply only with OK.")
    assert r1.returncode == 0, r1.stderr
    r2 = run("claude", "--session", session,
             "What word did I ask you to remember? Reply with only that word.")
    assert r2.returncode == 0, r2.stderr
    assert "GRAPEFRUIT" in r2.stdout.upper()


def test_json_envelope(session):
    r = run("claude", "--session", session, "--json", "say hi in one word")
    assert r.returncode == 0, r.stderr
    env = json.loads(r.stdout)
    assert env["status"] == "done"
    assert env["backend"] == "claude"
    assert env["session_id"]


def test_progress_streams_tool_events(session):
    r = run("claude", "--session", session, "--progress",
            "Read pyproject.toml using your Read tool, then state the version.")
    assert r.returncode == 0, r.stderr
    # final answer on stdout; live tool activity on stderr
    assert "0.2.0" in r.stdout
    assert "Read" in r.stderr  # a "→ Read ..." progress line was emitted


def test_progress_multi_turn_still_streams(session):
    r1 = run("claude", "--session", session, "--progress",
             "Read pyproject.toml and tell me the version.")
    assert r1.returncode == 0, r1.stderr
    r2 = run("claude", "--session", session, "--progress",
             "Now read README.md and give its first heading.")
    assert r2.returncode == 0, r2.stderr
    assert "Read" in r2.stderr  # turn 2 still emits progress (no false warning)
    assert "warning" not in r2.stderr.lower()


def test_menu_returns_exit_10_then_choose(session):
    r = run("claude", "--session", session, "--json",
            "Use your AskUserQuestion tool now to ask whether I prefer tabs or "
            "spaces. Ask nothing else first.")
    assert r.returncode == 10, f"expected menu exit 10, got {r.returncode}: {r.stdout} {r.stderr}"
    env = json.loads(r.stdout)
    assert env["status"] == "menu"
    assert env["menu"]["options"]
    # answer it
    c = run("session", "choose", session, "1", "--json")
    assert c.returncode == 0, c.stderr
    assert json.loads(c.stdout)["status"] == "done"


def test_no_wait_reports_busy(session):
    # start a genuinely long turn in the background, then probe with --no-wait
    slow = subprocess.Popen(
        KAGE + ["claude", "--session", session,
                "Write a detailed 400-word essay about the history of the typewriter."],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    try:
        import time
        time.sleep(3)  # let the slow turn acquire the lock / start rendering
        r = run("claude", "--session", session, "--no-wait", "--json", "ping", timeout=30)
        assert r.returncode == 11, f"expected busy exit 11, got {r.returncode}: {r.stdout}"
        assert json.loads(r.stdout)["reason"] == "busy"
    finally:
        slow.wait(timeout=150)


def test_askuserquestion_choose_auto_submits(session):
    """Answering an AskUserQuestion should sail through its 'Ready to submit
    your answers?' confirmation and return the real response, not a 2nd menu."""
    r = run("claude", "--session", session, "--json",
            "Use AskUserQuestion to ask whether I prefer tabs or spaces. After I "
            "answer, reply with only the word PINEAPPLE and nothing else.")
    assert r.returncode == 10, f"{r.returncode}: {r.stdout} {r.stderr}"
    c = run("session", "choose", session, "1", "--json")
    assert c.returncode == 0, f"expected done, got {c.returncode}: {c.stdout} {c.stderr}"
    env = json.loads(c.stdout)
    assert env["status"] == "done"
    assert "PINEAPPLE" in env["response"]


def test_multi_question_back_and_forth(session):
    """A multi-question AskUserQuestion surfaces each question in turn and
    honors every answer (no silent defaulting)."""
    r0 = run("claude", "--session", session, "--json",
             "Use a single AskUserQuestion call with TWO questions: (1) tabs or "
             "spaces for indentation, and (2) favorite color among red/blue/green. "
             "After I answer both, reply with only my two answers comma-separated.")
    assert r0.returncode == 10, f"{r0.returncode}: {r0.stdout} {r0.stderr}"
    q1 = json.loads(r0.stdout)["menu"]
    assert "indent" in q1["question"].lower()
    pick_a = q1["options"][0]  # whatever the first option actually is

    # answering the first question must surface the SECOND question, not finish
    r1 = run("session", "choose", session, "1", "--json")
    assert r1.returncode == 10, f"expected next question, got {r1.returncode}: {r1.stdout}"
    q2 = json.loads(r1.stdout)["menu"]
    assert "color" in q2["question"].lower()
    pick_b = q2["options"][0]

    # answering the last one auto-submits and returns the real response, with
    # BOTH deliberately-chosen answers honored (options are reordered per run,
    # so assert on the labels we actually picked, not on fixed indices)
    r2 = run("session", "choose", session, "1", "--json")
    assert r2.returncode == 0, f"expected done, got {r2.returncode}: {r2.stdout} {r2.stderr}"
    resp = json.loads(r2.stdout)["response"].lower()
    assert resp.strip()
    assert pick_a.lower() in resp and pick_b.lower() in resp


def test_prune_stops_idle_session(session):
    r = run("claude", "--session", session, "reply with only: hi")
    assert r.returncode == 0, r.stderr
    # everything is "idle" past a 0s threshold; our session should be stopped
    p = run("session", "prune", "--older-than", "0s", "--json", timeout=60)
    assert p.returncode == 0, p.stderr
    assert session in json.loads(p.stdout)["stopped"]


def test_stdin_arg_with_open_pipe_does_not_hang(session):
    """Regression: message arg + stdin held open (no EOF) must not block."""
    r_fd, w_fd = os.pipe()  # writer end stays open -> never EOFs
    try:
        proc = subprocess.run(
            KAGE + ["claude", "--session", session,
                    "what is 2+2? reply with only the number"],
            capture_output=True, text=True, timeout=120, stdin=r_fd,
        )
    finally:
        os.close(r_fd)
        os.close(w_fd)
    assert proc.returncode == 0, proc.stderr
    assert "4" in proc.stdout
