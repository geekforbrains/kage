"""Long-lived tmux-backed sessions wrapping a Backend."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

from . import tmux as tmuxlib
from .backends import Backend, Menu, State

SESSION_PREFIX = "kage_"


def _session_name(backend: str, name: str) -> str:
    return f"{SESSION_PREFIX}{backend}_{name}"


def parse_session_name(tmux_name: str) -> tuple[str, str] | None:
    """Return (backend, name) for a kage-managed tmux session, or None."""
    if not tmux_name.startswith(SESSION_PREFIX):
        return None
    rest = tmux_name[len(SESSION_PREFIX):]
    backend, _, name = rest.partition("_")
    return backend, name or "default"


@dataclass
class SendResult:
    state: State
    text: str = ""
    menu: Menu | None = None


class MenuPending(Exception):
    def __init__(self, menu: Menu):
        super().__init__(menu.question)
        self.menu = menu


class Session:
    """A persistent conversation with a single backend, running in tmux."""

    def __init__(
        self,
        name: str,
        backend: Backend,
        *,
        width: int = 200,
        height: int = 50,
    ):
        self.name = name
        self.backend = backend
        self.width = width
        self.height = height

    @property
    def tmux_name(self) -> str:
        return _session_name(self.backend.name, self.name)

    def __enter__(self) -> "Session":
        if not self.exists():
            self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def exists(self) -> bool:
        return tmuxlib.has_session(self.tmux_name)

    def start(self, *, system_prompt: str | None = None, ready_timeout: float = 15.0) -> None:
        if self.exists():
            raise RuntimeError(f"session already running: {self.tmux_name}")
        cmd = self.backend.start_command(system_prompt=system_prompt)
        tmuxlib.new_session(self.tmux_name, cmd, width=self.width, height=self.height)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self.backend.ready_marker(tmuxlib.capture(self.tmux_name)):
                return
            time.sleep(0.5)
        raise RuntimeError(f"backend did not become ready in {ready_timeout}s")

    def stop(self) -> bool:
        return tmuxlib.kill_session(self.tmux_name)

    def capture(self) -> str:
        return tmuxlib.capture(self.tmux_name)

    def _submit(self, text: str) -> int:
        """Paste text and submit. Returns the pre-submit done-marker count."""
        baseline = self.backend.done_marker_count(self.capture())
        tmuxlib.paste(self.tmux_name, text)
        time.sleep(0.25)
        tmuxlib.send_key(self.tmux_name, "Enter")
        return baseline

    def send(
        self,
        message: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.5,
    ) -> SendResult:
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        baseline = self._submit(message)
        return self._wait(baseline=baseline, timeout=timeout, poll_interval=poll_interval)

    def stream(
        self,
        message: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.4,
    ) -> Iterator[str]:
        """Yield new tail chunks as the response renders.

        Raises MenuPending if a menu blocks completion.
        """
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
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
                return SendResult(
                    state=State.DONE,
                    text=self.backend.extract_response(cur),
                )
            if self.backend.is_menu(cur):
                menu = self.backend.extract_menu(cur)
                if menu:
                    return SendResult(state=State.MENU, menu=menu)
        raise TimeoutError(f"no done marker after {timeout}s")

    def respond_to_menu(self, choice: int | str) -> SendResult:
        """Answer a pending menu. Integer choice selects by number; string is free text."""
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


def list_sessions() -> list[tuple[str, str]]:
    """Return [(backend, name)] for all kage-managed tmux sessions."""
    out = []
    for s in tmuxlib.list_sessions(prefix=SESSION_PREFIX):
        parsed = parse_session_name(s)
        if parsed:
            out.append(parsed)
    return out
