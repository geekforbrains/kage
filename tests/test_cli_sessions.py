from kage import cli
from kage import state as state_mod


def test_find_session_uses_running_slug_for_anonymous_match(monkeypatch):
    monkeypatch.setattr(state_mod, "get", lambda name: None)
    monkeypatch.setattr(cli, "list_sessions", lambda: [("claude", "foo_bar")])

    assert cli._find_session("foo bar") == ("claude", "foo_bar")
