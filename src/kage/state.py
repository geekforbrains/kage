"""Persistent state for named kage sessions.

Stores the mapping from a user-facing session name to its underlying backend
session UUID so that sessions survive reboots. Caller-managed sessions
(passed --session-id directly) bypass this entirely.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "kage"


def _state_file() -> Path:
    return _state_dir() / "sessions.json"


def lock_path(name: str) -> Path:
    d = _state_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.lock"


def events_path(session_id: str) -> Path:
    """Append-only hook-event log for a backend session (one line per firing)."""
    d = _state_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{session_id}.jsonl"


def hooks_settings_path(tmux_name: str) -> Path:
    """Per-session settings JSON registering kage's progress hooks."""
    d = _state_dir() / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{tmux_name}.json"


@dataclass
class SessionRecord:
    name: str
    backend: str
    session_id: str
    model: str | None = None
    effort: str | None = None
    system_prompt: str | None = None
    created_at: str = ""
    last_used_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class _Store:
    sessions: dict[str, SessionRecord] = field(default_factory=dict)


def _load() -> _Store:
    p = _state_file()
    if not p.exists():
        return _Store()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return _Store()
    sessions = {
        name: SessionRecord.from_dict(d)
        for name, d in raw.get("sessions", {}).items()
    }
    return _Store(sessions=sessions)


def _save(store: _Store) -> None:
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {"sessions": {n: r.to_dict() for n, r in store.sessions.items()}}
    p = _state_file()
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


def get(name: str) -> SessionRecord | None:
    return _load().sessions.get(name)


def upsert(record: SessionRecord) -> SessionRecord:
    store = _load()
    store.sessions[record.name] = record
    _save(store)
    return record


def remove(name: str) -> bool:
    store = _load()
    if name not in store.sessions:
        return False
    del store.sessions[name]
    _save(store)
    return True


def all_records() -> Iterator[SessionRecord]:
    return iter(_load().sessions.values())


def new_session_id() -> str:
    return str(uuid.uuid4())
