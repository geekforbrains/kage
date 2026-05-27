from __future__ import annotations

import pytest

from kage import tmux
from kage import cli


def _missing_tmux(*args, **kwargs):
    raise FileNotFoundError("tmux")


def test_tmux_missing_raises_clear_error(monkeypatch):
    monkeypatch.setattr(tmux.subprocess, "run", _missing_tmux)

    with pytest.raises(tmux.TmuxError) as exc:
        tmux.has_session("kage_claude_test")

    assert "tmux not found on PATH" in str(exc.value)


def test_cli_reports_missing_tmux_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(tmux.subprocess, "run", _missing_tmux)

    assert cli.main(["claude", "hello?"]) == cli.EXIT_ERROR

    captured = capsys.readouterr()
    assert "tmux not found on PATH" in captured.err
    assert "Traceback" not in captured.err


def test_new_session_shell_quotes_backend_command(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "tmux", lambda *args, **kwargs: calls.append(args))

    tmux.new_session(
        "kage_claude_quote",
        ["claude", "--append-system-prompt", "two words"],
    )

    assert calls[0][-1] == "claude --append-system-prompt 'two words'"
