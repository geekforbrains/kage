"""Low-level tmux wrappers."""
from __future__ import annotations

import os
import re
import shlex
import subprocess

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
TMUX_MISSING_MESSAGE = (
    "tmux not found on PATH. Install tmux (macOS: brew install tmux) "
    "and ensure it is available to the shell running kage."
)


class TmuxError(RuntimeError):
    """A tmux command failed before kage could talk to the backend."""


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise TmuxError(TMUX_MISSING_MESSAGE) from e


def tmux(*args: str, check: bool = True) -> str:
    r = _run_tmux(list(args))
    if check and r.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def has_session(name: str) -> bool:
    return _run_tmux(["has-session", "-t", name]).returncode == 0


def list_sessions(prefix: str = "") -> list[str]:
    r = _run_tmux(["list-sessions", "-F", "#{session_name}"])
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
    buffer_name = f"_kage_{os.getpid()}"
    tmux("set-buffer", "-b", buffer_name, "--", text)
    tmux("paste-buffer", "-b", buffer_name, "-t", name, "-d")


def send_key(name: str, key: str) -> None:
    tmux("send-keys", "-t", name, key)


def kill_session(name: str) -> bool:
    return _run_tmux(["kill-session", "-t", name]).returncode == 0


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_args(env: dict[str, str] | None) -> list[str]:
    if not env:
        return []
    args: list[str] = []
    for key, value in sorted(env.items()):
        if not _ENV_NAME_RE.match(key):
            continue
        args.extend(["-e", f"{key}={value}"])
    return args


def new_session(
    name: str,
    command: list[str],
    *,
    width: int = 500,
    height: int = 50,
    env: dict[str, str] | None = None,
) -> None:
    tmux(
        "new-session", "-d", "-s", name,
        *_env_args(env),
        "-x", str(width), "-y", str(height),
        shlex.join(command),
    )
