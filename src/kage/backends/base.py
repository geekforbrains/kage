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

    def start_command(self, *, system_prompt: str | None = None) -> list[str]:
        raise NotImplementedError

    def ready_marker(self, pane: str) -> bool:
        """True when the TUI has finished loading and is accepting input."""
        raise NotImplementedError

    def is_done(self, pane: str) -> bool:
        """True when the most recent response has finished rendering."""
        raise NotImplementedError

    def done_marker_count(self, pane: str) -> int:
        """How many completed responses are visible in the pane.

        Used to detect a new completion: the count increases by 1 per response.
        """
        raise NotImplementedError

    def is_menu(self, pane: str) -> bool:
        """True when the TUI is waiting on a menu choice."""
        raise NotImplementedError

    def extract_response(self, pane: str) -> str:
        """Pull the text of the most recent response from the pane."""
        raise NotImplementedError

    def extract_menu(self, pane: str) -> Menu | None:
        """Pull the pending menu's question and options."""
        raise NotImplementedError
