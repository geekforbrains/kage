from __future__ import annotations

import fcntl

import pytest

from kage import state as state_mod
from kage.backends import Backend, BackendError
from kage.session import BackendFailure, Session, SessionBusy, State


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, *, error: BackendError | None = None):
        self.error = error

    def start_command(self, **kwargs) -> list[str]:
        return ["fake"]

    def ready_marker(self, pane: str) -> bool:
        return True

    def is_done(self, pane: str) -> bool:
        return True

    def done_marker_count(self, pane: str) -> int:
        return 1

    def is_busy(self, pane: str) -> bool:
        return False

    def is_menu(self, pane: str) -> bool:
        return False

    def extract_response(self, pane: str) -> str:
        return ""

    def extract_menu(self, pane: str):
        return None

    def extract_error(self, pane: str) -> BackendError | None:
        return self.error


def test_wait_raises_backend_error():
    error = BackendError(message="not logged in", reason="not_logged_in")
    sess = Session(backend=FakeBackend(error=error), slug="x")
    sess.capture = lambda: "pane"  # type: ignore[method-assign]

    with pytest.raises(BackendFailure) as exc:
        sess._wait(baseline=0, timeout=0.01, poll_interval=0)

    assert exc.value.error is error


def test_wait_raises_empty_response_error():
    sess = Session(backend=FakeBackend(), slug="x")
    sess.capture = lambda: "pane"  # type: ignore[method-assign]

    with pytest.raises(BackendFailure) as exc:
        sess._wait(baseline=0, timeout=0.01, poll_interval=0)

    assert exc.value.error.reason == "empty_response"


def test_send_no_wait_respects_existing_lock():
    sess = Session(backend=FakeBackend(), slug="locked")
    sess.exists = lambda: True  # type: ignore[method-assign]

    lock = state_mod.lock_path(sess.tmux_name).open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with pytest.raises(SessionBusy):
            sess.send("hello", wait_if_busy=False)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


# --- session-crossover after `--restart`/resume -----------------------------
#
# After a pane restart, `claude --resume` re-renders the entire prior
# conversation. A stale done-marker from that scrollback can trip completion
# before this turn's answer exists, and pane-scraping it returns the PREVIOUS
# turn's response (the real-world bug: a sea-shanty answer surfaced for a
# later "why are the jobs timing out" question). When a session transcript is
# available it is the sole source of truth; the poisoned pane must never be the
# fallback.


class _FakeTail:
    """Minimal stand-in for hooks.EventTail."""

    def __init__(self, saw_stop: bool = False):
        self.saw_stop = saw_stop

    def poll(self):
        return []


class ResumeStaleBackend(FakeBackend):
    """A pane left full of the prior conversation after a resume: a stale
    done-marker and the previous answer are already on screen, while THIS
    turn's answer only lands in the transcript a few polls later."""

    STALE = "STALE: previous turn's shanty"
    FRESH = "FRESH: this turn's answer"

    def __init__(self, *, flush_after: int, has_done_marker: bool = True):
        super().__init__()
        self._calls = 0
        self._flush_after = flush_after
        self._has_done_marker = has_done_marker

    def done_marker_count(self, pane: str) -> int:
        return 1 if self._has_done_marker else 0

    def extract_response(self, pane: str) -> str:
        return self.STALE  # the poisoned, resumed pane

    def transcript_text_count(self, session_id: str) -> int:
        self._calls += 1
        return 2 if self._calls > self._flush_after else 1

    def final_response(self, session_id: str) -> str:
        return self.FRESH


def test_resume_stale_done_marker_returns_fresh_not_stale():
    """A stale done-marker must not pane-scrape the prior turn's answer; wait
    for the transcript to flush this turn's response."""
    sess = Session(
        backend=ResumeStaleBackend(flush_after=3),
        slug="x",
        session_id="uuid-done",
    )
    sess.capture = lambda: "pane"  # type: ignore[method-assign]

    res = sess._wait(
        baseline=0,        # done_marker_count(1) > 0 trips a stale 'done' at once
        timeout=5.0,
        poll_interval=0,
        tail=_FakeTail(saw_stop=False),
        on_event=lambda ev: None,
        resp_baseline=1,   # transcript still holds only the prior answer
    )
    assert res.state is State.DONE
    assert res.text == ResumeStaleBackend.FRESH


def test_resume_stale_stop_waits_for_transcript():
    """Same guard when completion arrives via a (possibly stale) Stop hook
    rather than a done-marker."""
    sess = Session(
        backend=ResumeStaleBackend(flush_after=3, has_done_marker=False),
        slug="x",
        session_id="uuid-stop",
    )
    sess.capture = lambda: "pane"  # type: ignore[method-assign]

    res = sess._wait(
        baseline=0,
        timeout=5.0,
        poll_interval=0,
        tail=_FakeTail(saw_stop=True),
        on_event=lambda ev: None,
        resp_baseline=1,
    )
    assert res.text == ResumeStaleBackend.FRESH


def test_ephemeral_without_session_still_pane_scrapes():
    """No session transcript (ephemeral / job path) keeps the pane scrape — the
    resume guard must not regress the one-shot path."""

    class PaneBackend(FakeBackend):
        def done_marker_count(self, pane: str) -> int:
            return 1

        def extract_response(self, pane: str) -> str:
            return "pane answer"

    sess = Session(backend=PaneBackend(), slug="x")  # session_id is None
    sess.capture = lambda: "pane"  # type: ignore[method-assign]

    res = sess._wait(
        baseline=0,
        timeout=1.0,
        poll_interval=0,
        tail=_FakeTail(),
        on_event=lambda ev: None,
        resp_baseline=0,
    )
    assert res.text == "pane answer"
