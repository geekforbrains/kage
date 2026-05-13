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
`--json` or `--output-format` for structured output, exit codes for status,
stdin for piped input.

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
elif result.returncode == 11:
    print("session was busy and --no-wait was set")
elif result.returncode == 124:
    print("timed out")
else:
    print("error:", result.stderr)
```

For systems that need to see tool calls and partial text as they happen,
use `--output-format stream-json` and parse one JSON line per event:

```json
{"type":"start","backend":"claude","session_id":"..."}
{"type":"tool_use","name":"Bash","input":"echo hello"}
{"type":"text","delta":"It printed hello."}
{"type":"done","duration_ms":4247}
```

## Two ways to identify a session

There are two persistence models. Pick the one that fits your caller.

**Named (kage-managed UUID):** `--session=name`. kage stores the underlying
conversation UUID in its own state file (`~/.local/state/kage/sessions.json`)
and reuses it on every call with that name. Survives reboots. Best for
humans and Enso-style scheduled jobs.

```bash
kage claude --session=work "read src/cli.py and explain"
kage claude --session=work "now refactor it"   # same conversation
```

**Caller-managed UUID:** `--session-id=<uuid>`. The caller owns the UUID,
kage just drives it. No state file involvement. Best for systems that
already track their own session lifecycles (e.g. Harbour).

```bash
kage claude --session-id=4c8b...69b6 "..."
kage claude --session-id=4c8b...69b6 "..."     # same conversation
```

Both modes survive a reboot because the underlying conversation file is
saved by the AI CLI itself.

## Session management

```bash
kage session list            # known and running sessions
kage session kill work       # stop tmux pane, keep the state record
kage session rm   work       # stop tmux pane and forget the state record
kage session show work       # raw pane dump (debug)
kage session menu work       # show pending menu, if any
kage session choose work 2   # answer a pending menu by option number
kage session compact work    # /compact: summarize the conversation
kage session clear work      # clear context: kill tmux + new UUID, same name
```

`clear` vs `compact`:

- `compact` keeps the same underlying conversation but summarizes its
  history. Useful when you're close to the context window limit.
- `clear` starts a brand new conversation under the same kage name. The
  old conversation file remains on disk but is no longer referenced.

## Options

`kage claude` (and other backend subcommands) accepts:

- `--session NAME`, `-s NAME` -- reuse a kage-managed persistent session
- `--session-id UUID` -- reuse a specific underlying conversation UUID
- `--timeout SECONDS`, `-t SECONDS` -- max wait for a response (default 120)
- `--system-prompt TEXT` -- appended to the backend's system prompt, on
  session start only
- `--model NAME` -- model alias to pass through (e.g. opus, sonnet)
- `--effort LEVEL` -- effort level to pass through (low/medium/high/xhigh/max)
- `--bare` -- pass `--bare` to the backend. **Caution:** for Claude Code
  this forces API-key mode and skips your subscription auth. Only use if
  you have `ANTHROPIC_API_KEY` set.
- `--output-format {text,json,stream-json}` -- output format (default text)
- `--json` -- shorthand for `--output-format=json`
- `--no-stream` -- never stream text, even to a tty
- `--no-wait` -- exit with code 11 instead of waiting if the session is
  mid-response from a previous call

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Error (session crashed, backend missing, etc.) |
| 2    | Usage error |
| 10   | Menu pending, awaiting input |
| 11   | Session busy and `--no-wait` was set |
| 124  | Timeout (no response within `--timeout` seconds) |

## JSON output

```bash
$ kage claude --json "what is 2+2"
{"status":"done","backend":"claude","session":null,"session_id":"...","response":"4"}
```

When the TUI is waiting on a menu (e.g. plan approval), you get:

```json
{"status":"menu","backend":"claude","session":"work",
 "session_id":"...",
 "menu":{"question":"Ready to code?","options":["...","..."]}}
```

The exit code is `10`. Respond with `kage session choose <name> <number>`.

## Library use

```python
from kage import Session, get_backend

# Three ways to construct a Session:
named = Session.named("work", get_backend("claude"))
by_id = Session.by_id("4c8b...-69b6", get_backend("claude"))
oneshot = Session.ephemeral(get_backend("claude"))

with named as s:
    print(s.send("hello").text)

    for chunk in s.stream("write a haiku"):
        print(chunk, end="", flush=True)

    for event in s.stream_events("count to 3"):
        print(event)
```

## Backends

| Backend  | Status |
|----------|--------|
| claude   | Working (Claude Code) |
| codex    | Planned (OpenAI Codex CLI) |
| gemini   | Planned (Google Gemini CLI) |

## How it works

1. `kage` spawns the target CLI inside a detached tmux session, pinned to a
   specific session UUID via `--session-id` (new) or `--resume` (existing).
2. Your message is loaded into a tmux paste buffer and pasted into the pane,
   then `Enter` is sent as a separate keystroke (sending them together is
   racy for some TUIs).
3. `kage` polls the rendered pane and watches for backend-specific markers
   (e.g. Claude's `Worked for Ns`) to know when the response is complete.
4. The response is parsed out of the rendered TUI and printed to stdout, or
   emitted as JSON events for `--output-format stream-json`.
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
