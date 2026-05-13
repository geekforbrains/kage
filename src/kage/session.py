"""Long-lived tmux-backed sessions wrapping a Backend."""
from __future__ import annotations

import datetime as _dt
import re
import time
import uuid
from dataclasses import dataclass
from typing import Iterator

from . import state as state_mod
from . import tmux as tmuxlib
from .backends import Backend, Menu, State

SESSION_PREFIX = "kage_"
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class SendResult:
    state: State
    text: str = ""
    menu: Menu | None = None


class MenuPending(Exception):
    def __init__(self, menu: Menu):
        super().__init__(menu.question)
        self.menu = menu


class SessionBusy(Exception):
    """The session is mid-response from a previous message."""


def _sanitize(s: str) -> str:
    out = _SANITIZE_RE.sub("_", s).strip("_")
    return out or "x"


def parse_session_name(tmux_name: str) -> tuple[str, str] | None:
    """Return (backend, slug) for a kage-managed tmux session, or None."""
    if not tmux_name.startswith(SESSION_PREFIX):
        return None
    rest = tmux_name[len(SESSION_PREFIX):]
    backend, _, slug = rest.partition("_")
    return backend, slug or "default"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class Session:
    """A persistent conversation with a single backend, running in tmux.

    There are three ways to identify a Session:
      1. By name (state-backed): `Session.named(name, backend)` looks up or
         creates a record in the kage state file. UUID is auto-managed.
      2. By explicit session_id (caller-managed): `Session.by_id(session_id,
         backend)` uses the UUID directly, no state file involvement.
      3. Ephemeral (one-shot): `Session.ephemeral(backend)` generates a
         random short-lived UUID, no state file involvement.
    """

    def __init__(
        self,
        *,
        backend: Backend,
        slug: str,
        session_id: str | None = None,
        name: str | None = None,
        record: state_mod.SessionRecord | None = None,
        width: int = 200,
        height: int = 50,
    ):
        self.backend = backend
        self.slug = slug
        self.session_id = session_id
        self.name = name
        self.record = record
        self.width = width
        self.height = height

    # ---- constructors ----

    @classmethod
    def named(
        cls,
        name: str,
        backend: Backend,
        *,
        bare: bool = False,
        model: str | None = None,
        effort: str | None = None,
        system_prompt: str | None = None,
    ) -> "Session":
        rec = state_mod.get(name)
        if rec is None:
            rec = state_mod.SessionRecord(
                name=name,
                backend=backend.name,
                session_id=state_mod.new_session_id(),
                bare=bare,
                model=model,
                effort=effort,
                system_prompt=system_prompt,
                created_at=_now(),
                last_used_at=_now(),
            )
            state_mod.upsert(rec)
        elif rec.backend != backend.name:
            raise RuntimeError(
                f"session {name!r} is bound to backend {rec.backend!r}, "
                f"not {backend.name!r}"
            )
        return cls(
            backend=backend,
            slug=_sanitize(name),
            session_id=rec.session_id,
            name=name,
            record=rec,
        )

    @classmethod
    def by_id(cls, session_id: str, backend: Backend) -> "Session":
        return cls(
            backend=backend,
            slug="id_" + session_id.replace("-", "")[:10],
            session_id=session_id,
            name=None,
            record=None,
        )

    @classmethod
    def ephemeral(cls, backend: Backend) -> "Session":
        sid = str(uuid.uuid4())
        return cls(
            backend=backend,
            slug="oneshot_" + sid.replace("-", "")[:8],
            session_id=sid,
            name=None,
            record=None,
        )

    # ---- naming / state ----

    @property
    def tmux_name(self) -> str:
        return f"{SESSION_PREFIX}{self.backend.name}_{self.slug}"

    def _display_name(self) -> str:
        if self.name:
            return f"kage:{self.backend.name}:{self.name}"
        return f"kage:{self.backend.name}:ephemeral"

    def __enter__(self) -> "Session":
        if not self.exists():
            self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def exists(self) -> bool:
        return tmuxlib.has_session(self.tmux_name)

    # ---- lifecycle ----

    def start(
        self,
        *,
        system_prompt: str | None = None,
        bare: bool | None = None,
        model: str | None = None,
        effort: str | None = None,
        ready_timeout: float = 20.0,
    ) -> None:
        if self.exists():
            raise RuntimeError(f"session already running: {self.tmux_name}")

        # Persisted record beats per-call defaults; per-call beats nothing.
        rec = self.record
        cfg_bare = bare if bare is not None else (rec.bare if rec else False)
        cfg_model = model or (rec.model if rec else None)
        cfg_effort = effort or (rec.effort if rec else None)
        cfg_prompt = system_prompt or (rec.system_prompt if rec else None)

        cmd = self.backend.start_command(
            session_id=self.session_id,
            display_name=self._display_name(),
            system_prompt=cfg_prompt,
            bare=cfg_bare,
            model=cfg_model,
            effort=cfg_effort,
        )
        tmuxlib.new_session(self.tmux_name, cmd, width=self.width, height=self.height)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self.backend.ready_marker(tmuxlib.capture(self.tmux_name)):
                if rec:
                    rec.last_used_at = _now()
                    state_mod.upsert(rec)
                return
            time.sleep(0.5)
        raise RuntimeError(f"backend did not become ready in {ready_timeout}s")

    def stop(self) -> bool:
        return tmuxlib.kill_session(self.tmux_name)

    def forget(self) -> None:
        """Remove the persisted record (no-op for unnamed sessions)."""
        if self.name:
            state_mod.remove(self.name)

    def reset(self) -> None:
        """Clear context: stop tmux, replace the UUID, leave to be restarted."""
        self.stop()
        new_id = state_mod.new_session_id()
        self.session_id = new_id
        if self.record:
            self.record.session_id = new_id
            self.record.last_used_at = _now()
            state_mod.upsert(self.record)

    def capture(self) -> str:
        return tmuxlib.capture(self.tmux_name)

    # ---- input ----

    def _submit(self, text: str) -> int:
        """Paste text and submit. Returns the pre-submit done-marker count."""
        baseline = self.backend.done_marker_count(self.capture())
        tmuxlib.paste(self.tmux_name, text)
        time.sleep(0.25)
        tmuxlib.send_key(self.tmux_name, "Enter")
        return baseline

    def is_busy(self) -> bool:
        if not self.exists():
            return False
        return self.backend.is_busy(self.capture())

    def send(
        self,
        message: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
        wait_if_busy: bool = True,
        wait_busy_timeout: float = 30.0,
    ) -> SendResult:
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        self._await_idle(wait_if_busy, wait_busy_timeout, poll_interval)
        baseline = self._submit(message)
        return self._wait(baseline=baseline, timeout=timeout, poll_interval=poll_interval)

    def stream(
        self,
        message: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.4,
        wait_if_busy: bool = True,
        wait_busy_timeout: float = 30.0,
    ) -> Iterator[str]:
        """Yield new text tail chunks as the response renders."""
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        self._await_idle(wait_if_busy, wait_busy_timeout, poll_interval)
        baseline = self._submit(message)
        last_full = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            cur = self.capture()
            partial = self.backend.extract_response(cur)
            if partial and partial != last_full:
                if partial.startswith(last_full):
                    yield partial[len(last_full):]
                else:
                    yield "\r" + partial
                last_full = partial
            if self.backend.done_marker_count(cur) > baseline:
                return
            if self.backend.is_menu(cur):
                menu = self.backend.extract_menu(cur)
                if menu:
                    raise MenuPending(menu)
        raise TimeoutError(f"no done marker after {timeout}s")

    def stream_events(
        self,
        message: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.4,
        wait_if_busy: bool = True,
        wait_busy_timeout: float = 30.0,
    ) -> Iterator[dict]:
        """Yield structured events as the response renders.

        Event types:
          - {"type": "start"}
          - {"type": "text", "delta": "..."}
          - {"type": "tool_use", "name": "Bash", "input": "..."}
          - {"type": "menu", "menu": {...}}
          - {"type": "done", "duration_ms": N}
        """
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        self._await_idle(wait_if_busy, wait_busy_timeout, poll_interval)
        started_at = time.time()
        baseline = self._submit(message)
        yield {"type": "start", "backend": self.backend.name, "session_id": self.session_id}

        last_full = ""
        seen_tools: set[tuple[str, str]] = set()
        deadline = started_at + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            cur = self.capture()
            partial = self.backend.extract_response(cur)
            if partial and partial != last_full:
                if partial.startswith(last_full):
                    yield {"type": "text", "delta": partial[len(last_full):]}
                else:
                    yield {"type": "text", "delta": partial, "replace": True}
                last_full = partial

            for tool in self.backend.extract_tool_uses(cur):
                key = (tool["name"], tool["input"])
                if key not in seen_tools:
                    seen_tools.add(key)
                    yield {"type": "tool_use", **tool}

            if self.backend.done_marker_count(cur) > baseline:
                yield {
                    "type": "done",
                    "duration_ms": int((time.time() - started_at) * 1000),
                }
                return
            if self.backend.is_menu(cur):
                menu = self.backend.extract_menu(cur)
                if menu:
                    yield {"type": "menu", "menu": {
                        "question": menu.question, "options": menu.options,
                    }}
                    return
        raise TimeoutError(f"no done marker after {timeout}s")

    def respond_to_menu(self, choice: int | str) -> SendResult:
        baseline = self.backend.done_marker_count(self.capture())
        if isinstance(choice, int):
            tmuxlib.send_key(self.tmux_name, str(choice))
            time.sleep(0.2)
            tmuxlib.send_key(self.tmux_name, "Enter")
        else:
            tmuxlib.paste(self.tmux_name, choice)
            time.sleep(0.25)
            tmuxlib.send_key(self.tmux_name, "Enter")
        return self._wait(baseline=baseline, timeout=120.0, poll_interval=0.5)

    def slash(self, command: str, *, timeout: float = 120.0) -> SendResult:
        """Send a slash command (e.g. /compact) and wait for completion."""
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        self._await_idle(True, 30.0, 0.5)
        self._submit(command)
        return self._wait_slash(command, timeout=timeout, poll_interval=0.5)

    def _wait_slash(
        self,
        command: str,
        *,
        timeout: float,
        poll_interval: float,
    ) -> SendResult:
        """Wait for a slash command to complete via its specific marker."""
        from .backends.claude import COMPACT_DONE_RE  # local import; backend-specific

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            cur = self.capture()
            if command.startswith("/compact"):
                if any(COMPACT_DONE_RE.match(l) for l in cur.splitlines()):
                    return SendResult(state=State.DONE, text=_compact_summary(cur))
            else:
                # Fall back to done-marker detection for unknown slash commands.
                if self.backend.is_done(cur):
                    return SendResult(
                        state=State.DONE,
                        text=self.backend.extract_response(cur),
                    )
        raise TimeoutError(f"slash command {command!r} did not complete in {timeout}s")

    # ---- internals ----

    def _await_idle(self, wait: bool, timeout: float, poll: float) -> None:
        if not self.backend.is_busy(self.capture()):
            return
        if not wait:
            raise SessionBusy(f"session {self.tmux_name} is mid-response")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            if not self.backend.is_busy(self.capture()):
                return
        raise SessionBusy(f"session {self.tmux_name} still busy after {timeout}s")

    def _wait(
        self,
        *,
        baseline: int,
        timeout: float,
        poll_interval: float,
    ) -> SendResult:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            cur = self.capture()
            if self.backend.done_marker_count(cur) > baseline:
                if self.record:
                    self.record.last_used_at = _now()
                    state_mod.upsert(self.record)
                return SendResult(
                    state=State.DONE,
                    text=self.backend.extract_response(cur),
                )
            if self.backend.is_menu(cur):
                menu = self.backend.extract_menu(cur)
                if menu:
                    return SendResult(state=State.MENU, menu=menu)
        raise TimeoutError(f"no done marker after {timeout}s")


def _compact_summary(pane: str) -> str:
    """Extract the human-visible summary line from a completed /compact."""
    for l in pane.splitlines():
        if "⎿" in l and "Compacted" in l:
            return l.strip().lstrip("⎿").strip()
    return ""


def list_sessions() -> list[tuple[str, str]]:
    """Return [(backend, slug)] for kage-managed tmux sessions currently running."""
    out = []
    for s in tmuxlib.list_sessions(prefix=SESSION_PREFIX):
        parsed = parse_session_name(s)
        if parsed:
            out.append(parsed)
    return out


def known_records() -> list[state_mod.SessionRecord]:
    return list(state_mod.all_records())
