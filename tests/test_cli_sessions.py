import argparse
import json

from kage import cli
from kage import state as state_mod


def test_find_session_uses_running_slug_for_anonymous_match(monkeypatch):
    monkeypatch.setattr(state_mod, "get", lambda name: None)
    monkeypatch.setattr(cli, "list_sessions", lambda: [("claude", "foo_bar")])

    assert cli._find_session("foo bar") == ("claude", "foo_bar")


def test_session_list_with_multiple_anonymous_running(monkeypatch, capsys):
    """Regression: two running anonymous (by-id) sessions and no records must
    not crash on slug_matches(None, ...)."""
    monkeypatch.setattr(
        cli, "list_sessions",
        lambda: [("claude", "id_294b879cbc"), ("claude", "id_2b6fcfa1bf")],
    )
    monkeypatch.setattr(cli, "known_records", lambda: [])

    args = argparse.Namespace(json=True)
    rc = cli.cmd_session_list(args)

    assert rc == cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    slugs = sorted(r["slug"] for r in rows)
    assert slugs == ["id_294b879cbc", "id_2b6fcfa1bf"]
    assert all(r["name"] is None and r["running"] for r in rows)


def test_session_list_dedupes_record_and_running(monkeypatch, capsys):
    """A named record that is also running appears once, not twice."""
    rec = state_mod.SessionRecord(
        name="work", backend="claude", session_id="sid-1",
    )
    monkeypatch.setattr(cli, "known_records", lambda: [rec])
    monkeypatch.setattr(cli, "list_sessions", lambda: [("claude", "work")])

    args = argparse.Namespace(json=True)
    assert cli.cmd_session_list(args) == cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["name"] == "work" and rows[0]["running"] is True


def test_slug_matches_handles_none():
    assert cli.slug_matches(None, "anything") is False
    assert cli.slug_matches("foo bar", "foo_bar") is True


class _FakeSess:
    session_id = "sid"

    def exists(self):
        return True

    def stop(self):
        return True


def _backend_args(**kwargs):
    base = dict(
        message="hi", session=None, session_id=None, timeout=10.0,
        model=None, effort=None, json=False, stream=False, restart=False,
        stop_on_signal=False, no_wait=False, system_prompt=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_ephemeral_installs_signal_cleanup(monkeypatch):
    """Ephemeral one-shots auto-install signal cleanup even without the flag,
    so an interrupted run never orphans its tmux pane."""
    installed = []
    monkeypatch.setattr(cli, "_build_session", lambda args, bn: (_FakeSess(), True))
    monkeypatch.setattr(
        cli, "_install_signal_cleanup",
        lambda sess: (installed.append(sess), (lambda: None))[1],
    )
    monkeypatch.setattr(cli, "_run_send", lambda *a, **k: cli.EXIT_OK)
    monkeypatch.setattr(cli, "_tmux_env", lambda: {})

    rc = cli.cmd_backend(_backend_args(stop_on_signal=False), "claude")
    assert rc == cli.EXIT_OK
    assert installed, "ephemeral session must auto-install signal cleanup"


def test_persistent_session_no_signal_cleanup_without_flag(monkeypatch):
    """Named/by-id sessions are meant to persist, so they only install signal
    cleanup when the caller opts in via --stop-on-signal."""
    installed = []
    monkeypatch.setattr(cli, "_build_session", lambda args, bn: (_FakeSess(), False))
    monkeypatch.setattr(
        cli, "_install_signal_cleanup",
        lambda sess: (installed.append(sess), (lambda: None))[1],
    )
    monkeypatch.setattr(cli, "_run_send", lambda *a, **k: cli.EXIT_OK)
    monkeypatch.setattr(cli, "_tmux_env", lambda: {})

    rc = cli.cmd_backend(_backend_args(stop_on_signal=False), "claude")
    assert rc == cli.EXIT_OK
    assert not installed, "persistent session must not auto-install cleanup"


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
