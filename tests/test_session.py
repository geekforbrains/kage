from __future__ import annotations

import fcntl

import pytest

from kage import state as state_mod
from kage.backends import Backend, BackendError
from kage.session import BackendFailure, Session, SessionBusy


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
