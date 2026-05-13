# kage

The missing `-p` mode for subscription AI CLIs.

`kage` ("shadow" in Japanese) lets you call interactive AI terminal tools like
Claude Code, Codex, and Gemini as if they were ordinary command-line programs.
Under the hood it drives a long-lived tmux session, so the underlying CLI
behaves as if a human were typing into it. Output comes back on stdout,
errors on stderr, exit code reflects success.

```bash
$ kage claude "what is 2+2?"
4
```

## Why

Some AI CLIs only expose their non-interactive (`-p`) mode behind a paid API
key. If you're a subscription user, you're stuck with the interactive TUI.
`kage` bridges that gap, giving you a real scriptable CLI on top of any
interactive AI tool, without an API key.

## Install

```bash
pipx install kage-cli     # recommended
# or
pip install kage-cli
```

Requirements: `tmux` 3.0 or newer, Python 3.11+, and the underlying agent CLI
you want to control (e.g. `claude`) logged into your subscription.

Run `kage doctor` to verify your environment.

## Usage

### One-shot

```bash
kage claude "summarize the README at ./README.md in one line"
```

Each call starts a fresh session, runs your message, and tears down. Just
like `claude -p` but on your subscription.

### Stdin

`kage` follows the usual Unix conventions: if stdin is piped, it's appended
to your message as context. If you don't pass a message at all, stdin
becomes the message.

```bash
cat report.md | kage claude "summarize this in 5 bullets"
echo "what is the capital of france" | kage claude
```

### Persistent sessions

Pass `--session NAME` to keep context across calls:

```bash
kage claude --session=work "read src/cli.py and explain the entry point"
kage claude --session=work "now refactor the dispatch function"
kage claude --session=work "run the tests"
```

Manage sessions:

```bash
kage session list           # list running sessions
kage session kill work      # terminate a session
kage session show work      # dump the raw pane (debug)
```

### JSON output

For scripts that need structure:

```bash
$ kage claude --json "what is 2+2"
{"status":"done","backend":"claude","session":null,"response":"4"}
```

Menu (e.g. plan approval) responses come back as:

```json
{"status":"menu","menu":{"question":"Ready to code?","options":["...","..."]}}
```

Exit code `10` indicates a menu is pending. Answer it with:

```bash
kage session choose work 2
```

### System prompt injection

Useful for shaping behavior in automation (no plan mode, no clarifying
questions, etc.):

```bash
kage claude \
  --system-prompt "no plan mode. no clarifying questions. return only the answer." \
  "implement the function in todo.py"
```

`--system-prompt` only takes effect when starting a new session.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Error (session crashed, backend missing, etc.) |
| 2    | Usage error |
| 10   | Menu pending, awaiting input |
| 124  | Timeout (no response in `--timeout` seconds) |

## Library use

```python
from kage import Session, get_backend

with Session("work", get_backend("claude")) as s:
    print(s.send("hello").text)
    for chunk in s.stream("write a haiku"):
        print(chunk, end="", flush=True)
```

## Supported backends

- **claude** (Claude Code) - working
- **codex** (OpenAI Codex CLI) - planned
- **gemini** (Google Gemini CLI) - planned

## How it works

1. `kage` spawns the target CLI inside a detached tmux session.
2. The message is loaded into a tmux paste buffer, pasted into the pane,
   then `Enter` is sent as a separate keystroke.
3. `kage` polls the rendered pane and watches for backend-specific markers
   (e.g. Claude's `Worked for Ns`) to know when the response is complete.
4. The response text is parsed out of the TUI render and printed to stdout.
5. Interactive menus (plan approval, etc.) are returned as a structured
   `MenuPending` result with exit code 10, so callers can choose how to
   respond.

## What this isn't

- Not a wrapper around the API. `kage` only drives the interactive CLI.
- Not a credential broker. Your subscription auth lives where the CLI
  already keeps it.
- Not magic. If the TUI changes its output format, `kage` may need updating.

## License

MIT
