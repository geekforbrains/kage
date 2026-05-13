# kage

Scriptable bridge for interactive AI CLIs.

`kage` ("shadow" in Japanese) wraps interactive terminal AI tools like Claude
Code, Codex, and Gemini, exposing them as a stable command-line interface that
scripts and automation can call. Under the hood it drives a long-lived tmux
session, so the underlying CLI behaves as if a human were typing into it.

## Why

Some AI CLIs (notably Claude Code) require an interactive TUI when run on a
personal subscription. Their non-interactive `-p` modes only work with a paid
API key. `kage` lets you keep using the subscription-backed TUI from
automation, by sending messages into a persistent tmux session and parsing
the structured output that comes back.

## Supported backends

- **claude** (Claude Code) - working
- **codex** (OpenAI Codex CLI) - planned
- **gemini** (Google Gemini CLI) - planned

## Install

```bash
pip install kage-cli
```

Requirements: `tmux` 3.0 or newer, Python 3.11+, and the underlying agent CLI
you want to control (e.g. `claude`).

## Usage

Single-shot (fresh session, run, return, cleanup):

```bash
kage run claude "summarize the README at ./README.md in one line"
```

Persistent session (multi-turn, faster):

```bash
kage start claude --name work
kage send work "what files are in src/?"
kage send work "now read kage/cli.py and explain the main entry point"
kage stop work
```

List running sessions:

```bash
kage list
```

Stream output as it arrives:

```bash
kage stream claude "write a haiku about tmux"
```

## Library use

```python
from kage import Session, get_backend

backend = get_backend("claude")
with Session("work", backend) as s:
    print(s.send("hello"))
    for chunk in s.stream("write a haiku"):
        print(chunk, end="", flush=True)
```

## How it works

1. `kage start` spawns the target CLI inside a detached tmux session.
2. `kage send` loads the message into a tmux paste buffer, pastes it into the
   pane, and sends `Enter` as a separate keystroke.
3. The wrapper polls the rendered pane, watching for a backend-specific "done"
   marker (e.g. Claude Code's `✻ Worked for Ns`) and confirming the pane has
   stabilized.
4. If a confirmation menu appears instead, `kage` returns a structured
   `MenuPending` result with the question and options, so the caller can
   decide what to do.

## Status

Alpha. The Claude Code backend works for single and multi-turn use. Menu
handling and additional backends are in progress.

## License

MIT
