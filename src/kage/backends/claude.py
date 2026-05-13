"""Claude Code backend."""
from __future__ import annotations

import re

from .base import Backend, Menu

PROMPT_RE = re.compile(r"^❯[\xa0 ]")
DONE_RE = re.compile(r"^✻ [A-Za-z]+ for \d+s")
MENU_OPTION_RE = re.compile(r"^\s*(?:❯[\xa0 ])?\s*(\d+)\.\s+(.+?)\s*$")
MENU_HEADERS = (
    "Ready to code?",
    "Enable auto mode?",
    "Would you like to proceed?",
    "Trust the files in this folder?",
)


class ClaudeBackend(Backend):
    name = "claude"

    def start_command(self, *, system_prompt: str | None = None) -> list[str]:
        cmd = ["claude", "--dangerously-skip-permissions"]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        return cmd

    def ready_marker(self, pane: str) -> bool:
        return any(PROMPT_RE.match(l) for l in pane.splitlines())

    def is_done(self, pane: str) -> bool:
        return any(DONE_RE.match(l) for l in pane.splitlines())

    def done_marker_count(self, pane: str) -> int:
        return sum(1 for l in pane.splitlines() if DONE_RE.match(l))

    def is_menu(self, pane: str) -> bool:
        lines = pane.splitlines()
        if not any(h in pane for h in MENU_HEADERS):
            return False
        return any(MENU_OPTION_RE.match(l) for l in lines)

    def extract_response(self, pane: str) -> str:
        lines = pane.splitlines()
        last_done = -1
        for i, l in enumerate(lines):
            if DONE_RE.match(l):
                last_done = i
        if last_done < 0:
            return ""
        # The user's message echo is the last `❯ ...` line BEFORE the done marker.
        last_prompt = -1
        for i in range(last_done - 1, -1, -1):
            if PROMPT_RE.match(lines[i]):
                last_prompt = i
                break
        if last_prompt < 0:
            return ""
        body = lines[last_prompt + 1 : last_done]
        cleaned = []
        for l in body:
            if _is_tool_chrome(l):
                continue
            if l.startswith("⏺ "):
                l = l[2:]
            elif l.startswith("  "):
                l = l[2:]
            cleaned.append(l)
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return "\n".join(cleaned)

    def extract_menu(self, pane: str) -> Menu | None:
        lines = pane.splitlines()
        question = ""
        for h in MENU_HEADERS:
            for l in lines:
                if h in l:
                    question = h.rstrip("?") + "?"
                    break
            if question:
                break
        options: list[str] = []
        for l in lines:
            m = MENU_OPTION_RE.match(l)
            if m:
                options.append(m.group(2))
        if not options:
            return None
        return Menu(question=question or "Choose an option", options=options, raw=pane)


def _is_tool_chrome(line: str) -> bool:
    """Strip Claude's collapsed tool-use lines and TUI hints."""
    s = line.strip()
    if not s:
        return False
    if s.startswith("⎿ "):
        return True
    if s.endswith("(ctrl+o to expand)"):
        return True
    return False
