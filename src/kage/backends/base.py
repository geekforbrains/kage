"""Backend protocol shared by all wrapped CLIs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    WORKING = "working"
    DONE = "done"
    MENU = "menu"


@dataclass
class Menu:
    question: str
    options: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class BackendError:
    message: str
    reason: str = "backend_error"
    raw: str = ""


class Backend:
    """Subclass per CLI tool. Provides start command and state detection."""

    name: str = ""

    def start_command(
        self,
        *,
        session_id: str | None = None,
        display_name: str | None = None,
        system_prompt: str | None = None,
        bare: bool = False,
        model: str | None = None,
        effort: str | None = None,
        settings: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def ready_marker(self, pane: str) -> bool:
        raise NotImplementedError

    def is_done(self, pane: str) -> bool:
        raise NotImplementedError

    def done_marker_count(self, pane: str) -> int:
        raise NotImplementedError

    def is_busy(self, pane: str) -> bool:
        """True when the backend is mid-response (more user messages than completions)."""
        raise NotImplementedError

    def is_menu(self, pane: str) -> bool:
        raise NotImplementedError

    def extract_response(self, pane: str) -> str:
        raise NotImplementedError

    def extract_menu(self, pane: str) -> Menu | None:
        raise NotImplementedError

    def is_multi_question(self, pane: str) -> bool:
        """True if the pane shows a multi-question/tabbed prompt UI.

        Such UIs select-and-advance on a bare keypress, so kage must not append
        Enter when answering (Enter would submit with defaults). Default: False.
        """
        return False

    def auto_submit_option(self, menu: Menu) -> int | str | None:
        """For a pure-confirmation menu the caller's choice already implies,
        return the option to auto-select; None to surface the menu instead.

        Default: never auto-submit. Backends override for ceremony menus like
        AskUserQuestion's "Ready to submit your answers?" review step.
        """
        return None

    def extract_error(self, pane: str) -> BackendError | None:
        """Return a backend-level diagnostic visible in the current pane."""
        return None

    def final_response(self, session_id: str) -> str | None:
        """Final assistant text for a session from its own transcript, if any.

        Read-only structured source (no rendering loss), used by the hook-driven
        progress path. Returns None when the backend has no transcript concept.
        """
        return None

    def transcript_text_count(self, session_id: str) -> int:
        """Count of assistant text messages in the transcript (0 if none/N/A).

        Baselined before a turn so `final_response` can be distinguished from a
        stale prior answer (see the claude backend).
        """
        return 0
