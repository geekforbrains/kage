"""Menu states are surfaced as non-interactive failures."""
import argparse
import json

from kage import hooks
from kage import cli
from kage.backends import Menu, State
from kage.session import SendResult


class FakeSession:
    class Backend:
        def __init__(self, *, busy=False, menu=False):
            self.busy = busy
            self.menu = menu

        def is_busy(self, pane):
            return self.busy

        def is_menu(self, pane):
            return self.menu

    def __init__(self, result, *, exists=True, events=None, has_hooks=False,
                 busy=False, menu=False):
        self._result = result
        self._exists = exists
        self._has_hooks = has_hooks
        self.events = events or []
        self.backend = self.Backend(busy=busy, menu=menu)
        self.session_id = "fake-sid-1234"
        self.slug = "oneshot_fake1234"
        self.stopped = False
        self.started = False
        self.start_progress = None
        self.start_kwargs = None
        self.send_kwargs = None

    def exists(self):
        return self._exists

    def has_progress_hooks(self):
        return self._has_hooks

    def start(self, **kwargs):
        self.started = True
        self.start_progress = kwargs.get("progress")
        self.start_kwargs = kwargs
        self._exists = True
        self._has_hooks = bool(self.start_progress)

    def capture(self):
        return "pane"

    def send(self, *a, **k):
        self.send_kwargs = k
        if k.get("on_event"):
            for event in self.events:
                k["on_event"](event)
        return self._result

    def stop(self):
        self.stopped = True
        return True


def _args(**kw):
    base = dict(
        message="hi", session=None, session_id=None,
        json=False, stream=False, timeout=120.0, model=None, effort=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _patch_build(monkeypatch, sess, cleanup_after):
    monkeypatch.setattr(cli, "_build_session", lambda args, bn: (sess, cleanup_after))


def test_ephemeral_menu_is_torn_down(monkeypatch):
    sess = FakeSession(SendResult(state=State.MENU,
                                  menu=Menu(question="Which one?", options=["a", "b"])))
    _patch_build(monkeypatch, sess, cleanup_after=True)
    rc = cli.cmd_backend(_args(), "claude")
    assert rc == cli.EXIT_INTERACTION_REQUIRED
    assert sess.stopped is True


def test_ephemeral_done_is_cleaned_up(monkeypatch):
    sess = FakeSession(SendResult(state=State.DONE, text="hi"))
    _patch_build(monkeypatch, sess, cleanup_after=True)
    rc = cli.cmd_backend(_args(), "claude")
    assert rc == cli.EXIT_OK
    assert sess.stopped is True  # ordinary one-shot still tidies up


def test_menu_error_does_not_expose_choices(monkeypatch, capsys):
    sess = FakeSession(SendResult(state=State.MENU,
                                  menu=Menu(question="Which one?", options=["a", "b"])))
    _patch_build(monkeypatch, sess, cleanup_after=True)
    cli.cmd_backend(_args(), "claude")
    err = capsys.readouterr().err
    assert "interactive TUI prompt" in err
    assert "Which one?" in err
    assert "1. a" not in err


def test_stream_mode_emits_progress_jsonl(monkeypatch, capsys):
    event = hooks.HookEvent(ts=123.0, event="PreToolUse", tool="Bash", summary="ls")
    sess = FakeSession(SendResult(state=State.DONE, text="done"), events=[event],
                       has_hooks=True)
    _patch_build(monkeypatch, sess, cleanup_after=False)

    rc = cli.cmd_backend(_args(stream=True), "claude")

    assert rc == cli.EXIT_OK
    assert sess.send_kwargs["on_event"] is not None
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["status"] == "progress"
    assert lines[0]["event"] == "PreToolUse"
    assert lines[0]["tool"] == "Bash"
    assert lines[0]["summary"] == "ls"
    assert lines[1]["status"] == "done"
    assert lines[1]["response"] == "done"


def test_stream_mode_starts_new_session_with_progress_hooks(monkeypatch):
    sess = FakeSession(SendResult(state=State.DONE, text="done"), exists=False)
    _patch_build(monkeypatch, sess, cleanup_after=False)

    rc = cli.cmd_backend(_args(stream=True), "claude")

    assert rc == cli.EXIT_OK
    assert sess.started is True
    assert sess.start_progress is True


def test_start_passes_enso_origin_env(monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C123")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    monkeypatch.setenv("UNRELATED_SECRET", "ignored")
    sess = FakeSession(SendResult(state=State.DONE, text="done"), exists=False)
    _patch_build(monkeypatch, sess, cleanup_after=False)

    rc = cli.cmd_backend(_args(), "claude")

    assert rc == cli.EXIT_OK
    assert sess.start_kwargs["env"] == {
        "ENSO_ORIGIN_CHANNEL": "C123",
        "ENSO_ORIGIN_THREAD_TS": "1700.1",
    }


def test_stream_mode_restarts_idle_existing_session_without_hooks(monkeypatch):
    sess = FakeSession(SendResult(state=State.DONE, text="done"))
    _patch_build(monkeypatch, sess, cleanup_after=False)

    rc = cli.cmd_backend(_args(stream=True), "claude")

    assert rc == cli.EXIT_OK
    assert sess.stopped is True
    assert sess.started is True
    assert sess.start_progress is True
