"""Ephemeral-session menu handling: don't tear down an unanswered menu."""
import argparse

from kage import cli
from kage.backends import Menu, State
from kage.session import SendResult


class FakeSession:
    def __init__(self, result):
        self._result = result
        self.session_id = "fake-sid-1234"
        self.slug = "oneshot_fake1234"
        self.stopped = False

    def exists(self):
        return True

    def has_progress_hooks(self):
        return False

    def send(self, *a, **k):
        return self._result

    def stop(self):
        self.stopped = True
        return True


def _args(**kw):
    base = dict(
        message="hi", session=None, session_id=None,
        output_format=None, json=False, timeout=120.0, no_wait=False,
        progress=False, bare=False, system_prompt=None, model=None, effort=None,
        autonomous=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _patch_build(monkeypatch, sess, cleanup_after):
    monkeypatch.setattr(cli, "_build_session", lambda args, bn: (sess, cleanup_after))


def test_ephemeral_menu_is_not_torn_down(monkeypatch):
    sess = FakeSession(SendResult(state=State.MENU,
                                  menu=Menu(question="Which one?", options=["a", "b"])))
    _patch_build(monkeypatch, sess, cleanup_after=True)
    rc = cli.cmd_backend(_args(), "claude")
    assert rc == cli.EXIT_MENU
    assert sess.stopped is False  # the menu must remain answerable


def test_ephemeral_done_is_cleaned_up(monkeypatch):
    sess = FakeSession(SendResult(state=State.DONE, text="hi"))
    _patch_build(monkeypatch, sess, cleanup_after=True)
    rc = cli.cmd_backend(_args(), "claude")
    assert rc == cli.EXIT_OK
    assert sess.stopped is True  # ordinary one-shot still tidies up


def test_menu_guidance_uses_slug_for_anonymous(monkeypatch, capsys):
    sess = FakeSession(SendResult(state=State.MENU,
                                  menu=Menu(question="Which one?", options=["a", "b"])))
    _patch_build(monkeypatch, sess, cleanup_after=True)
    cli.cmd_backend(_args(), "claude")  # text mode -> guidance on stderr
    err = capsys.readouterr().err
    assert "kage session choose oneshot_fake1234" in err
    assert "<name>" not in err
