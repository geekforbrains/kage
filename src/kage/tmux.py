"""Low-level tmux wrappers."""
from __future__ import annotations

import re
import subprocess

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def tmux(*args: str, check: bool = True) -> str:
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def has_session(name: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", name], capture_output=True
    ).returncode == 0


def list_sessions(prefix: str = "") -> list[str]:
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    names = [n for n in r.stdout.splitlines() if n]
    return [n for n in names if n.startswith(prefix)] if prefix else names


def capture(name: str, *, history: bool = True) -> str:
    # `-J` joins wrapped lines (so output >pane width round-trips correctly),
    # but as a side effect preserves the trailing spaces tmux uses to pad
    # short lines. Strip them per-line so callers see clean text.
    args = ["capture-pane", "-t", name, "-p", "-J"]
    if history:
        args += ["-S", "-"]
    raw = strip_ansi(tmux(*args))
    return "\n".join(l.rstrip() for l in raw.splitlines())


def paste(name: str, text: str) -> None:
    """Load text into a buffer and paste it into the target pane."""
    tmux("set-buffer", "-b", "_kage", "--", text)
    tmux("paste-buffer", "-b", "_kage", "-t", name, "-d")


def send_key(name: str, key: str) -> None:
    tmux("send-keys", "-t", name, key)


def kill_session(name: str) -> bool:
    return subprocess.run(
        ["tmux", "kill-session", "-t", name], capture_output=True
    ).returncode == 0


def new_session(name: str, command: list[str], *, width: int = 500, height: int = 50) -> None:
    tmux(
        "new-session", "-d", "-s", name,
        "-x", str(width), "-y", str(height),
        " ".join(command),
    )
