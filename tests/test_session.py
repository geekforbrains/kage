from __future__ import annotations

import pytest

from kage.backends import Backend, BackendError
from kage.session import BackendFailure, Session


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
