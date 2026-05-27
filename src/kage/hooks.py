"""Claude Code hook integration for live progress and done-detection.

kage registers a small set of hooks via a per-session ``--settings`` file. Each
hook invokes ``kage hook <event> --events-file <path>``, which appends one JSON
record per firing to an append-only log. The session tails that log to surface
live tool-use progress and to detect turn completion via the ``Stop`` event —
all read-only, so it never changes how a request is billed (requests are still
submitted by typing into the interactive chat).
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Hooks we register. PreToolUse/PostToolUse fire mid-turn (live progress);
# Stop fires once the turn completes (done-detection). Stop deliberately stays
# silent while a menu/AskUserQuestion is pending, so menu detection remains a
# separate, pane-based signal.
_PROGRESS_EVENTS = ("PreToolUse", "PostToolUse", "Stop")


@dataclass
class HookEvent:
    ts: float
    event: str
    tool: str = ""
    summary: str = ""

    @property
    def is_stop(self) -> bool:
        return self.event == "Stop"


def _hook_command(events_file: Path, event_name: str) -> str:
    exe = shlex.quote(sys.executable)
    path = shlex.quote(str(events_file))
    # Invoke the package entrypoint so it works regardless of console-script
    # installation: `python -m kage hook <event> --events-file <path>`.
    return f"{exe} -m kage hook {event_name} --events-file {path}"


def build_settings(events_file: Path) -> dict:
    """Settings dict registering kage's progress hooks against `events_file`."""
    hooks: dict[str, list] = {}
    for ev in _PROGRESS_EVENTS:
        entry: dict = {"hooks": [{"type": "command", "command": _hook_command(events_file, ev)}]}
        if ev in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = "*"
        hooks[ev] = [entry]
    return {"hooks": hooks}


def write_settings_file(settings_file: Path, events_file: Path) -> None:
    settings_file.write_text(json.dumps(build_settings(events_file), indent=2))


def _summarize_tool_input(tool: str, tool_input) -> str:
    """One-line, human-friendly summary of what a tool is doing."""
    if not isinstance(tool_input, dict):
        return ""
    if tool == "AskUserQuestion":
        questions = tool_input.get("questions") or []
        if questions and isinstance(questions[0], dict):
            return str(questions[0].get("question", ""))[:120]
        return ""
    for key in ("command", "file_path", "pattern", "url", "path", "description", "prompt"):
        val = tool_input.get(key)
        if val:
            return str(val)[:120]
    return ""


def record_from_payload(event_name: str, payload: dict) -> HookEvent:
    tool = payload.get("tool_name", "") or ""
    summary = _summarize_tool_input(tool, payload.get("tool_input"))
    return HookEvent(ts=round(time.time(), 3), event=event_name, tool=tool, summary=summary)


def handle_hook(event_name: str, events_file: Path, stdin=None) -> int:
    """Entry point for `kage hook`. Best-effort; never fails the calling CLI."""
    stream = stdin if stdin is not None else sys.stdin
    try:
        raw = stream.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rec = record_from_payload(event_name, payload)
    try:
        events_file.parent.mkdir(parents=True, exist_ok=True)
        with events_file.open("a") as f:
            f.write(json.dumps(rec.__dict__) + "\n")
            f.flush()
    except Exception:
        return 0  # never break the backend on a logging failure
    return 0


class EventTail:
    """Incrementally reads new hook-event records appended to a log file."""

    def __init__(self, path: Path):
        self.path = path
        self._offset = 0
        self._buf = ""
        self.saw_stop = False

    def seek_to_end(self) -> None:
        """Baseline at the current file size so prior turns are ignored."""
        try:
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0
        self._buf = ""

    def poll(self) -> list[HookEvent]:
        """Return event records appended since the last poll."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:  # file rotated/truncated — restart
            self._offset = 0
            self._buf = ""
        if size == self._offset:
            return []
        try:
            with self.path.open("r") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            return []
        self._buf += chunk
        out: list[HookEvent] = []
        lines = self._buf.split("\n")
        self._buf = lines.pop()  # trailing partial line (if any)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ev = HookEvent(
                ts=d.get("ts", 0.0),
                event=d.get("event", ""),
                tool=d.get("tool", ""),
                summary=d.get("summary", ""),
            )
            if ev.is_stop:
                self.saw_stop = True
            out.append(ev)
        return out
