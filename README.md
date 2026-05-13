# kage

The missing `-p` mode for subscription AI CLIs.

`kage` ("shadow" in Japanese) lets you call interactive AI terminal tools
like Claude Code as if they were ordinary command-line programs. Under the
hood it drives a long-lived tmux session, so the underlying CLI behaves as
if a human were typing into it. Output goes to stdout, errors to stderr,
exit code reflects success.

```bash
$ kage claude "what is 2+2?"
4
```

## Why

Some AI CLIs only expose their non-interactive (`-p`) mode behind a paid API
key. If you use the tool on a personal subscription, you're stuck with the
interactive TUI. `kage` bridges that gap: a real scriptable CLI on top of
the interactive tool, no API key required.

## Install

```bash
pipx install kage-cli     # recommended
# or
pip install kage-cli
```

Requirements:

- `tmux` 3.0 or newer
- Python 3.11+
- The underlying AI CLI you want to control (e.g. `claude`), already logged
  into your subscription

Run `kage doctor` to verify your environment.

## Quick usage

One-shot, just like `claude -p` would be:

```bash
kage claude "summarize the README at ./README.md in one line"
```

Pipe stdin in as context:

```bash
cat report.md | kage claude "summarize this in 5 bullets"
```

Or let stdin be the whole message:

```bash
echo "what is the capital of france" | kage claude
```

Streaming output, to a terminal, is automatic. When stdout is piped, `kage`
waits for the full response and prints it once.

## Calling from scripts

`kage` is built to be called from automation. The contract is stable:
`--json` for structured output, exit codes for status, stdin for piped
input.

```python
import subprocess, json

result = subprocess.run(
    ["kage", "claude", "--json",
     "--system-prompt",
     "no plan mode. no clarifying questions. return only the answer."],
    input="implement the function in todo.py",
    capture_output=True, text=True, timeout=300,
)

if result.returncode == 0:
    data = json.loads(result.stdout)
    print(data["response"])
elif result.returncode == 10:
    print("blocked on a menu, see stderr")
elif result.returncode == 124:
    print("timed out")
else:
    print("error:", result.stderr)
```

## Persistent sessions

Pass `--session NAME` to keep context across calls:

```bash
kage claude --session=work "read src/cli.py and explain the entry point"
kage claude --session=work "now refactor the dispatch function"
kage claude --session=work "run the tests"
```

Manage them:

```bash
kage session list           # list running sessions
kage session kill work      # terminate a session
kage session show work      # raw pane dump (debug)
kage session menu work      # show pending menu, if any
kage session choose work 2  # answer a pending menu by option number
```

## Options

`kage claude` (and other backend subcommands) accepts:

- `--session NAME`, `-s NAME` -- reuse a persistent session across calls
- `--timeout SECONDS`, `-t SECONDS` -- max wait for a response (default 120)
- `--system-prompt TEXT` -- appended to the backend's system prompt, on
  session start only
- `--json` -- emit a JSON envelope instead of plain text
- `--no-stream` -- always print the full response at the end, never stream

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Error (session crashed, backend missing, etc.) |
| 2    | Usage error |
| 10   | Menu pending, awaiting input |
| 124  | Timeout (no response within `--timeout` seconds) |

## JSON output

```bash
$ kage claude --json "what is 2+2"
{"status":"done","backend":"claude","session":null,"response":"4"}
```

When the TUI is waiting on a menu (e.g. plan approval), you get:

```json
{"status":"menu","backend":"claude","session":"work",
 "menu":{"question":"Ready to code?","options":["...","..."]}}
```

The exit code is `10`. Respond with `kage session choose <name> <number>`.

## Library use

```python
from kage import Session, get_backend

with Session("work", get_backend("claude")) as s:
    print(s.send("hello").text)
    for chunk in s.stream("write a haiku"):
        print(chunk, end="", flush=True)
```

## Backends

| Backend  | Status |
|----------|--------|
| claude   | Working (Claude Code) |
| codex    | Planned (OpenAI Codex CLI) |
| gemini   | Planned (Google Gemini CLI) |

## How it works

1. `kage` spawns the target CLI inside a detached tmux session.
2. Your message is loaded into a tmux paste buffer and pasted into the pane,
   then `Enter` is sent as a separate keystroke (sending them together is
   racy for some TUIs).
3. `kage` polls the rendered pane and watches for backend-specific markers
   (e.g. Claude's `Worked for Ns`) to know when the response is complete.
4. The response is parsed out of the rendered TUI and printed to stdout.
5. Interactive menus (plan approval, etc.) are returned as a structured
   envelope with exit code 10, so callers can decide how to respond.

## What this isn't

- Not a wrapper around the API. `kage` only drives the interactive CLI.
- Not a credential broker. Your subscription auth stays wherever the
  underlying CLI already keeps it.
- Not magic. If the TUI changes its output format, `kage` may need
  updating.

## License

MIT
