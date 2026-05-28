import argparse
import json

from kage import cli
from kage import state as state_mod


def test_find_session_uses_running_slug_for_anonymous_match(monkeypatch):
    monkeypatch.setattr(state_mod, "get", lambda name: None)
    monkeypatch.setattr(cli, "list_sessions", lambda: [("claude", "foo_bar")])

    assert cli._find_session("foo bar") == ("claude", "foo_bar")


def test_session_target_accepts_session_id():
    sid = "11111111-2222-3333-4444-555555555555"
    args = argparse.Namespace(name=None, session_id=sid, backend="claude")

    backend_name, label, sess = cli._session_for_target(args)

    assert backend_name == "claude"
    assert label == "id:11111111"
    assert sess.session_id == sid
    assert sess.slug == "id_1111111122"


def test_session_target_rejects_name_and_session_id(capsys):
    args = argparse.Namespace(name="work", session_id="sid", backend="claude")

    assert cli._session_for_target(args) is None
    assert "mutually exclusive" in capsys.readouterr().err


def test_session_control_parser_accepts_session_id():
    parser = cli._build_parser()

    args = parser.parse_args([
        "session", "kill", "--session-id",
        "11111111-2222-3333-4444-555555555555",
    ])

    assert args.name is None
    assert args.session_id == "11111111-2222-3333-4444-555555555555"
    assert args.backend == "claude"
    assert args.func is cli.cmd_session_kill


class FakeControlSession:
    def __init__(self, *, exists=True):
        self.session_id = "old-session-id"
        self._exists = exists
        self.stopped = False
        self.reset_called = False

    def stop(self):
        self.stopped = True
        return self._exists

    def exists(self):
        return self._exists

    def capture(self):
        return "pane contents"

    def reset(self):
        self.reset_called = True
        self.stop()
        self.session_id = "new-session-id"


def _target_args(**kwargs):
    base = {
        "name": None,
        "session_id": "old-session-id",
        "backend": "claude",
        "json": False,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_session_kill_targets_session_id(monkeypatch, capsys):
    sess = FakeControlSession()
    monkeypatch.setattr(cli, "_session_for_target", lambda args: ("claude", "id:old-sess", sess))

    rc = cli.cmd_session_kill(_target_args())

    assert rc == cli.EXIT_OK
    assert sess.stopped is True
    assert "stopped: claude/id:old-sess" in capsys.readouterr().out


def test_session_show_targets_session_id(monkeypatch, capsys):
    sess = FakeControlSession()
    monkeypatch.setattr(cli, "_session_for_target", lambda args: ("claude", "id:old-sess", sess))

    rc = cli.cmd_session_show(_target_args())

    assert rc == cli.EXIT_OK
    assert capsys.readouterr().out == "pane contents"


def test_session_clear_targets_session_id(monkeypatch, capsys):
    sess = FakeControlSession()
    monkeypatch.setattr(cli, "_session_for_target", lambda args: ("claude", "id:old-sess", sess))

    rc = cli.cmd_session_clear(_target_args(json=True))

    assert rc == cli.EXIT_OK
    assert sess.reset_called is True
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "cleared",
        "session": None,
        "session_id": "new-session-id",
        "cleared_session_id": "old-session-id",
    }
