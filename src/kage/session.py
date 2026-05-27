"""Long-lived tmux-backed sessions wrapping a Backend."""
from __future__ import annotations

import datetime as _dt
import fcntl
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from . import hooks as hooks_mod
from . import state as state_mod
from . import tmux as tmuxlib
from .backends import Backend, BackendError, Menu, State

# Grace period after a Stop hook fires for the pane/transcript to settle before
# we give up trying to extract the response.
_STOP_GRACE = 4.0

SESSION_PREFIX = "kage_"
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class SendResult:
    state: State
    text: str = ""
    menu: Menu | None = None


class SessionBusy(Exception):
    """The session is mid-response from a previous message."""


class BackendFailure(Exception):
    def __init__(self, error: BackendError):
        super().__init__(error.message)
        self.error = error


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
        width: int = 500,
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

    @property
    def events_file(self):
        """Append-only hook-event log for this session's underlying UUID."""
        return state_mod.events_path(self.session_id or "anon")

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

    def has_progress_hooks(self) -> bool:
        """True if this session was started with kage's progress hooks."""
        return state_mod.hooks_settings_path(self.tmux_name).exists()

    # ---- lifecycle ----

    def start(
        self,
        *,
        system_prompt: str | None = None,
        bare: bool | None = None,
        model: str | None = None,
        effort: str | None = None,
        progress: bool = False,
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

        settings = None
        if progress and not cfg_bare:
            # --bare skips hooks entirely, so progress only applies otherwise.
            settings_file = state_mod.hooks_settings_path(self.tmux_name)
            hooks_mod.write_settings_file(settings_file, self.events_file)
            settings = str(settings_file)

        cmd = self.backend.start_command(
            session_id=self.session_id,
            display_name=self._display_name(),
            system_prompt=cfg_prompt,
            bare=cfg_bare,
            model=cfg_model,
            effort=cfg_effort,
            settings=settings,
        )
        tmuxlib.new_session(self.tmux_name, cmd, width=self.width, height=self.height)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            pane = tmuxlib.capture(self.tmux_name)
            # A pending menu at startup (e.g. trust-folder prompt) is also a
            # valid "ready" signal — the caller can answer it via the menu
            # flow rather than this raising a generic timeout.
            if self.backend.ready_marker(pane) or self.backend.is_menu(pane):
                if rec:
                    rec.last_used_at = _now()
                    state_mod.upsert(rec)
                return
            time.sleep(0.5)
        raise RuntimeError(f"backend did not become ready in {ready_timeout}s")

    def stop(self) -> bool:
        killed = tmuxlib.kill_session(self.tmux_name)
        # Drop the per-session hook artifacts so a later non-progress session
        # reusing this name isn't mistaken for a progress-enabled one.
        for p in (state_mod.hooks_settings_path(self.tmux_name), self.events_file):
            try:
                p.unlink()
            except OSError:
                pass
        return killed

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
        on_event=None,
    ) -> SendResult:
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        with self._locked(wait_if_busy, wait_busy_timeout, poll_interval):
            # A menu pending from a previous turn or from startup must be
            # answered before any new message is sent; pasting into a menu
            # would type the message into the option selector.
            pane = self.capture()
            if self.backend.is_menu(pane):
                menu = self.backend.extract_menu(pane)
                if menu:
                    return SendResult(state=State.MENU, menu=menu)
            self._await_idle(wait_if_busy, wait_busy_timeout, poll_interval)
            tail = None
            resp_baseline = 0
            if on_event is not None:
                tail = hooks_mod.EventTail(self.events_file)
                tail.seek_to_end()
                if self.session_id:
                    # Count existing answers so a stale prior one isn't returned
                    # if Stop fires before this turn's text is flushed.
                    resp_baseline = self.backend.transcript_text_count(self.session_id)
            baseline = self._submit(message)
            return self._wait(
                baseline=baseline,
                timeout=timeout,
                poll_interval=poll_interval,
                tail=tail,
                on_event=on_event,
                resp_baseline=resp_baseline,
            )

    def respond_to_menu(self, choice: int | str, *, timeout: float = 120.0) -> SendResult:
        with self._locked(True, 30.0, 0.5):
            baseline = self.backend.done_marker_count(self.capture())
            if isinstance(choice, int):
                tmuxlib.send_key(self.tmux_name, str(choice))
                time.sleep(0.2)
                tmuxlib.send_key(self.tmux_name, "Enter")
            else:
                tmuxlib.paste(self.tmux_name, choice)
                time.sleep(0.25)
                tmuxlib.send_key(self.tmux_name, "Enter")
            # Three valid terminal states for a menu response:
            #   1. A new response is generated → `✻ Verb for Ns` marker appears.
            #   2. A nested menu replaces the current one.
            #   3. The menu is dismissed without producing a response (e.g. plan
            #      mode's "Tell Claude what to change") and Claude is idle.
            deadline = time.time() + timeout
            idle_since: float | None = None
            while time.time() < deadline:
                time.sleep(0.5)
                cur = self.capture()
                self._raise_backend_error(cur)
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
                    idle_since = None
                    continue
                if not self.backend.is_busy(cur):
                    # Menu dismissed, no new response, claude back to idle.
                    # Require the state to hold briefly to avoid catching a
                    # gap between menu disappearance and spinner appearance.
                    if idle_since is None:
                        idle_since = time.time()
                    elif time.time() - idle_since > 1.5:
                        return SendResult(state=State.DONE, text="")
                else:
                    idle_since = None
            raise TimeoutError(f"no terminal state after {timeout}s")

    def slash(self, command: str, *, timeout: float = 120.0) -> SendResult:
        """Send a slash command (e.g. /compact) and wait for completion."""
        if not self.exists():
            raise RuntimeError(f"no session: {self.tmux_name}")
        with self._locked(True, 30.0, 0.5):
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
            self._raise_backend_error(cur)
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

    @contextmanager
    def _locked(self, wait: bool, timeout: float, poll: float):
        path = state_mod.lock_path(self.tmux_name)
        fh = path.open("a")
        acquired = False
        deadline = time.time() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if not wait or time.time() >= deadline:
                        raise SessionBusy(
                            f"session {self.tmux_name} is locked by another kage call"
                        )
                    time.sleep(poll)
            yield
        finally:
            if acquired:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

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
        tail=None,
        on_event=None,
        resp_baseline: int = 0,
    ) -> SendResult:
        deadline = time.time() + timeout
        stop_seen_at: float | None = None
        while time.time() < deadline:
            time.sleep(poll_interval)
            cur = self.capture()
            self._raise_backend_error(cur)

            # Surface live progress events as they're appended by the hooks.
            if tail is not None:
                for ev in tail.poll():
                    if on_event is not None:
                        on_event(ev)
                if tail.saw_stop and stop_seen_at is None:
                    stop_seen_at = time.time()

            # A menu pauses the turn (the Stop hook deliberately won't fire);
            # detect it from the pane regardless of mode.
            if self.backend.is_menu(cur):
                menu = self.backend.extract_menu(cur)
                if menu:
                    return SendResult(state=State.MENU, menu=menu)

            # Done either via the Stop hook (preferred when present) or the
            # rendered done-marker. Prefer the transcript for the response text
            # (verbatim), falling back to the pane scrape.
            done = self.backend.done_marker_count(cur) > baseline
            if stop_seen_at is not None or done:
                # Progress mode (tail set) prefers the verbatim transcript, but
                # only once this turn's answer has actually been flushed (the
                # text count advanced past the pre-submit baseline) — otherwise
                # we'd return the previous turn's stale answer. The default path
                # stays pure pane-scraping for clean A/B.
                text = None
                if tail is not None and self.session_id:
                    if self.backend.transcript_text_count(self.session_id) > resp_baseline:
                        text = self.backend.final_response(self.session_id)
                if not text:
                    text = self.backend.extract_response(cur)
                if not text:
                    # Stop fired but neither source has text yet; allow the
                    # pane/transcript a brief grace before declaring empty.
                    if stop_seen_at is not None and not done \
                            and time.time() - stop_seen_at < _STOP_GRACE:
                        continue
                    self._raise_empty_response(cur)
                if self.record:
                    self.record.last_used_at = _now()
                    state_mod.upsert(self.record)
                return SendResult(state=State.DONE, text=text)
        raise TimeoutError(f"no done marker after {timeout}s")

    def _raise_backend_error(self, pane: str) -> None:
        error = self.backend.extract_error(pane)
        if error:
            raise BackendFailure(error)

    def _raise_empty_response(self, pane: str) -> None:
        raise BackendFailure(BackendError(
            message="backend completed without a detectable response",
            reason="empty_response",
            raw=pane,
        ))


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
