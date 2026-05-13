"""Claude Code backend."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .base import Backend, Menu

PROMPT_RE = re.compile(r"^❯[\xa0 ]")
DONE_RE = re.compile(r"^✻ [A-Za-z]+ for \d+s")
MENU_OPTION_RE = re.compile(r"^\s*(?:❯[\xa0 ])?\s*(\d+)\.\s+(.+?)\s*$")
TOOL_CALL_RE = re.compile(r"^⏺ ([A-Z][A-Za-z0-9_]*)\((.*?)\)\s*$")
RESPONSE_MARKER = "⏺ "
COMPACT_DONE_RE = re.compile(r"^\s*⎿\s+Compacted\b")
MENU_HEADERS = (
    "Ready to code?",
    "Enable auto mode?",
    "Would you like to proceed?",
    "Trust the files in this folder?",
)


def _has_prompt_content(line: str) -> bool:
    """Detect a `❯ ...` line where the prompt actually contains a message."""
    if not PROMPT_RE.match(line):
        return False
    return bool(line[2:].strip())


class ClaudeBackend(Backend):
    name = "claude"

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
        cmd = ["claude", "--dangerously-skip-permissions"]
        if session_id:
            if _conversation_file(session_id).exists():
                cmd += ["--resume", session_id]
            else:
                cmd += ["--session-id", session_id]
        if display_name:
            cmd += ["--name", display_name]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if bare:
            cmd.append("--bare")
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def ready_marker(self, pane: str) -> bool:
        return any(PROMPT_RE.match(l) for l in pane.splitlines())

    def is_done(self, pane: str) -> bool:
        return any(DONE_RE.match(l) for l in pane.splitlines())

    def done_marker_count(self, pane: str) -> int:
        return sum(1 for l in pane.splitlines() if DONE_RE.match(l))

    def is_busy(self, pane: str) -> bool:
        lines = pane.splitlines()
        prompts = sum(1 for l in lines if _has_prompt_content(l))
        completions = self.done_marker_count(pane)
        # If a menu is visible, we are not "busy" in the response sense.
        if self.is_menu(pane):
            return False
        return prompts > completions

    def is_menu(self, pane: str) -> bool:
        if not any(h in pane for h in MENU_HEADERS):
            return False
        return any(MENU_OPTION_RE.match(l) for l in pane.splitlines())

    def extract_response(self, pane: str) -> str:
        lines = pane.splitlines()
        last_done = -1
        for i, l in enumerate(lines):
            if DONE_RE.match(l):
                last_done = i
        if last_done < 0:
            return ""
        last_prompt = -1
        for i in range(last_done - 1, -1, -1):
            if PROMPT_RE.match(lines[i]):
                last_prompt = i
                break
        if last_prompt < 0:
            return ""
        response_start = -1
        for i in range(last_prompt + 1, last_done):
            if lines[i].startswith(RESPONSE_MARKER):
                response_start = i
                break
        if response_start < 0:
            return ""
        body = lines[response_start : last_done]
        cleaned = []
        for l in body:
            if _is_tool_chrome(l):
                continue
            if l.startswith(RESPONSE_MARKER):
                l = l[len(RESPONSE_MARKER):]
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
                    question = h
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

    def extract_tool_uses(self, pane: str) -> list[dict]:
        out = []
        for l in pane.splitlines():
            m = TOOL_CALL_RE.match(l)
            if m:
                out.append({"name": m.group(1), "input": m.group(2)})
        return out


def _conversation_file(session_id: str) -> Path:
    """Where Claude stores the JSONL log for a session, scoped to current cwd.

    Claude rewrites the cwd by replacing both `/` and `.` with `-`, so e.g.
    `/Users/me/.enso/workspace` becomes `-Users-me--enso-workspace`.
    """
    sanitized = os.getcwd().replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"


def _is_tool_chrome(line: str) -> bool:
    """Strip Claude's collapsed tool-use lines and TUI hints."""
    s = line.strip()
    if not s:
        return False
    if s.startswith("⎿ "):
        return True
    if s.endswith("(ctrl+o to expand)"):
        return True
    if TOOL_CALL_RE.match(line):
        return True
    return False
