"""TDD for session GC: duration parsing, idle planning, orphan reaping."""
import argparse
import datetime as dt

import pytest

from kage import cli
from kage import session as session_mod
from kage import state as state_mod
from kage.session import (
    find_orphan_artifacts,
    parse_duration,
    plan_idle_records,
)


# --- parse_duration -------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("45s", 45),
    ("30m", 1800),
    ("2h", 7200),
    ("3d", 259200),
    ("120", 120),      # bare number = seconds
    ("0s", 0),
])
def test_parse_duration_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "5x", "-3h", "1.5h", "h"])
def test_parse_duration_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# --- plan_idle_records ----------------------------------------------------

def _rec(name, last_used_at):
    return state_mod.SessionRecord(
        name=name, backend="claude", session_id=f"id-{name}",
        created_at=last_used_at, last_used_at=last_used_at,
    )


def test_plan_idle_records_selects_only_stale():
    now = dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    fresh = _rec("fresh", (now - dt.timedelta(hours=1)).isoformat())
    stale = _rec("stale", (now - dt.timedelta(hours=30)).isoformat())
    ttl = 24 * 3600
    out = plan_idle_records([fresh, stale], now, ttl)
    names = {r.name for r in out}
    assert names == {"stale"}


def test_plan_idle_records_skips_undated():
    now = dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    undated = _rec("undated", "")
    assert plan_idle_records([undated], now, 0) == []


def test_plan_idle_records_boundary_is_exclusive():
    now = dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    exactly = _rec("x", (now - dt.timedelta(seconds=100)).isoformat())
    # idle == ttl is not "older than" ttl
    assert plan_idle_records([exactly], now, 100) == []
    assert {r.name for r in plan_idle_records([exactly], now, 99)} == {"x"}


# --- find_orphan_artifacts ------------------------------------------------

def test_find_orphan_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # hooks file for a running session (keep) and a dead one (orphan)
    live = state_mod.hooks_settings_path("kage_claude_live")
    dead = state_mod.hooks_settings_path("kage_claude_dead")
    live.write_text("{}")
    dead.write_text("{}")
    # events: one fresh (keep), one stale by mtime (orphan)
    import os
    import time
    fresh_ev = state_mod.events_path("fresh-sid")
    stale_ev = state_mod.events_path("stale-sid")
    fresh_ev.write_text("")
    stale_ev.write_text("")
    old = time.time() - 10 * 24 * 3600
    os.utime(stale_ev, (old, old))

    orphans = find_orphan_artifacts(
        running_tmux_names={"kage_claude_live"},
        now=time.time(),
        ttl=24 * 3600,
    )
    orphan_set = set(orphans)
    assert dead in orphan_set            # hooks file with no live session
    assert stale_ev in orphan_set        # events log older than ttl
    assert live not in orphan_set        # running session's hooks kept
    assert fresh_ev not in orphan_set    # recent events log kept


# --- cmd_session_prune (command wiring) -----------------------------------

def _prune_args(**kw):
    base = {"older_than": "24h", "dry_run": False, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_prune_removes_orphan_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "known_records", lambda: [])
    monkeypatch.setattr(session_mod, "list_sessions", lambda: [])
    dead = state_mod.hooks_settings_path("kage_claude_dead")
    dead.write_text("{}")

    rc = cli.cmd_session_prune(_prune_args(json=True))
    assert rc == 0
    assert not dead.exists()  # orphan removed
    out = capsys.readouterr().out
    assert "kage_claude_dead" in out


def test_cmd_prune_dry_run_keeps_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "known_records", lambda: [])
    monkeypatch.setattr(session_mod, "list_sessions", lambda: [])
    dead = state_mod.hooks_settings_path("kage_claude_dead")
    dead.write_text("{}")

    rc = cli.cmd_session_prune(_prune_args(dry_run=True))
    assert rc == 0
    assert dead.exists()  # dry-run leaves files in place


def test_cmd_prune_rejects_bad_duration():
    rc = cli.cmd_session_prune(_prune_args(older_than="nonsense"))
    assert rc == cli.EXIT_USAGE


def test_cmd_prune_stops_stale_sessions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(session_mod, "list_sessions", lambda: [])
    now = dt.datetime.now(dt.timezone.utc)
    stale = _rec("oldsesh", (now - dt.timedelta(days=3)).isoformat())
    monkeypatch.setattr(cli, "known_records", lambda: [stale])

    stopped = []
    monkeypatch.setattr(session_mod.Session, "stop",
                        lambda self: stopped.append(self.name) or True)

    rc = cli.cmd_session_prune(_prune_args(json=True))
    assert rc == 0
    assert stopped == ["oldsesh"]
