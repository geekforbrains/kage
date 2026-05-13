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

    def extract_tool_uses(self, pane: str) -> list[dict]:
        """Return tool-use events visible in the current pane.

        Each dict has at least: {"name": str, "input": str}. Order matches
        rendering order; callers may need to dedupe.
        """
        return []
