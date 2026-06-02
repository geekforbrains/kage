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


def test_askuserquestion_is_not_surfaced_as_menu(session):
    r = run("claude", "--session", session, "--json",
            "Use your AskUserQuestion tool now to ask whether I prefer tabs or "
            "spaces. Ask nothing else first.")
    assert r.returncode == 0, f"expected autonomous answer, got {r.returncode}: {r.stdout} {r.stderr}"
    env = json.loads(r.stdout)
    assert env["status"] == "done"
    assert "AskUserQuestion" in env["response"]


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


def _stream_done(stdout):
    """Last `status=done` envelope from a --stream run, or None."""
    done = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("status") == "done":
            done = ev
    return done


def test_restart_stream_returns_fresh_not_previous_answer(session):
    """Regression for the session-crossover bug.

    `--restart` kills the pane and `claude --resume`s, re-rendering the prior
    turn into the pane before this turn runs. The streamed answer must be THIS
    turn's, not the previous turn's response scraped from stale scrollback (the
    real-world symptom: a sea-shanty answer surfaced for a later jobs question).
    Uses --stream because that is the path Enso drives chat through and the one
    the crossover lived on.
    """
    r1 = run("claude", "--session", session, "--stream",
             "Reply with exactly one word: APRICOT. Nothing else.")
    assert r1.returncode == 0, r1.stderr
    d1 = _stream_done(r1.stdout)
    assert d1 and "APRICOT" in d1["response"].upper(), r1.stdout

    r2 = run("claude", "--session", session, "--stream", "--restart",
             "Forget the previous word. Reply with exactly one word: ZUCCHINI. "
             "Nothing else.")
    assert r2.returncode == 0, r2.stderr
    d2 = _stream_done(r2.stdout)
    assert d2, r2.stdout
    resp = d2["response"].upper()
    assert "ZUCCHINI" in resp, f"crossed/stale response after restart: {d2['response']!r}"
    assert "APRICOT" not in resp, f"previous turn's answer leaked: {d2['response']!r}"
