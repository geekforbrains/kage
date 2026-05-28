# kage

`claude -p`-style automation through the subscription-backed TUI.

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

`claude -p` used to be the easy way to run one-off agentic commands from
scripts while using your Claude subscription. As Claude Code's print mode
moved to separate API-style billing, subscription users were left with the
interactive TUI as the path that still uses their plan.

`kage` restores the `claude -p` shape without invoking print mode: it drives
Claude Code through a real TUI in tmux, then returns the final response like a
normal command-line program.

## Install

Not on PyPI yet. Clone and install from source:

```bash
git clone https://github.com/geekforbrains/kage.git
cd kage
pipx install .              # recommended
# or, for a development checkout:
pip install -e .
```

Once a release is cut it will be published as `kage-cli` on PyPI
(`pipx install kage-cli`).

Requirements:

- `tmux` 3.0 or newer
- Python 3.11+
- The underlying AI CLI you want to control (e.g. `claude`), already logged
  into your subscription

Run `kage doctor` to verify your environment.

## Quick usage

One-shot, `claude -p`-style, while still driving Claude through its
interactive TUI:

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

By default, `kage` waits for Claude to finish and prints the final response
once. This keeps the ordinary command shape close to `claude -p`. If a caller
needs live progress while the turn runs, use `--stream` for newline-delimited
JSON progress events followed by the final response envelope.

For supervised services that already track conversation IDs, bind kage to a
caller-owned Claude session and make process interruption clean:

```bash
kage claude \
  --stream \
  --stop-on-signal \
  --timeout 1800 \
  --restart \
  --session-id "$CLAUDE_SESSION_ID" \
  --model opus \
  "handle this request"
```

`--restart` gives each request a fresh tmux pane for the same Claude
conversation UUID, which is useful when a parent service wants predictable
hooks and environment propagation. `--stop-on-signal` stops the pane if the
supervising kage process receives `SIGINT` or `SIGTERM`.

## Calling from scripts

`kage` is built to be called from automation. The contract is stable:
`--json` for one final structured envelope, `--stream` for JSONL progress,
exit codes for status, stdin for piped input.

```python
import subprocess, json

result = subprocess.run(
    ["kage", "claude", "--json"],
    input="implement the function in todo.py",
    capture_output=True, text=True, timeout=300,
)

if result.returncode == 0:
    data = json.loads(result.stdout)
    print(data["response"])
elif result.returncode == 10:
    print("claude paused on an interactive TUI prompt")
elif result.returncode == 11:
    print("session was busy")
elif result.returncode == 124:
    print("timed out")
else:
    print("error:", result.stderr)
```

For long-running calls, read `--stream` line by line:

```python
import json
import subprocess

proc = subprocess.Popen(
    [
        "kage", "claude",
        "--stream",
        "--stop-on-signal",
        "--timeout", "1800",
        "inspect this repo and run the tests",
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

for line in proc.stdout:
    event = json.loads(line)
    if event["status"] == "progress":
        print(event["event"], event["tool"], event["summary"])
    elif event["status"] == "done":
        print(event["response"])

code = proc.wait()
```

`--stream` exposes hook-level progress such as tool start/finish events. It
does not expose partial assistant text deltas from Claude's renderer.
The final `done` envelope is based on Claude's terminal transcript response;
pre-tool narration such as "I'll check that" is treated as progress and not
returned as the final answer.

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
kage session compact work    # /compact: summarize the conversation
kage session clear work      # clear context: kill tmux + new UUID, same name
kage session prune --older-than 24h --dry-run
                              # show idle panes/orphan hook files to prune
```

For caller-managed sessions, lifecycle commands that operate on a live pane
also accept `--session-id`:

```bash
kage session show  --session-id "$CLAUDE_SESSION_ID"
kage session kill  --session-id "$CLAUDE_SESSION_ID"
kage session clear --session-id "$CLAUDE_SESSION_ID" --json
```

`clear` vs `compact`:

- `compact` keeps the same underlying conversation but summarizes its
  history. Useful when you're close to the context window limit.
- `clear` starts a brand new conversation under the same kage name. The
  old conversation file remains on disk but is no longer referenced.
- `prune` stops idle tmux panes and reaps orphaned hook/event artifacts.
  Use `--dry-run` first if you want to see what would be touched.

## Options

`kage claude` accepts:

- `--session NAME`, `-s NAME` -- reuse a kage-managed persistent session
- `--session-id UUID` -- reuse a specific underlying conversation UUID
- `--timeout SECONDS`, `-t SECONDS` -- max wait for a response (default 120)
- `--model NAME` -- model alias to pass through (e.g. opus, sonnet)
- `--effort LEVEL` -- effort level to pass through (low/medium/high/xhigh/max)
- `--json` -- emit a JSON envelope instead of plain response text
- `--stream` -- emit newline-delimited JSON progress events and final envelope
- `--restart` -- restart an existing tmux pane before sending
- `--stop-on-signal` -- stop the tmux pane if kage receives SIGINT/SIGTERM

Autonomous behavior is always on. kage removes Claude Code interaction tools
that would pause for TUI input (`AskUserQuestion`, `EnterPlanMode`,
`ExitPlanMode`) and appends instructions to make reasonable assumptions
instead of asking.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Error (session crashed, backend missing, not logged in, empty response, etc.) |
| 2    | Usage error |
| 10   | Claude paused on an interactive TUI prompt |
| 11   | Session busy |
| 124  | Timeout (no response within `--timeout` seconds) |

## JSON output

```bash
$ kage claude --json "what is 2+2"
{"status":"done","backend":"claude","session":null,"session_id":"...","response":"4"}
```

If Claude still reaches an interactive TUI prompt, JSON mode returns an error
envelope and exits `10`:

```json
{"status":"error","backend":"claude","session":"work",
 "session_id":"...",
 "reason":"interaction_required",
 "message":"Claude Code paused on an interactive TUI prompt..."}
```

This should be rare in normal use because kage starts Claude with permission
bypass enabled and interaction tools denied. When it happens, inspect or reset
the session with `kage session show <name>` or `kage session rm <name>`.

When the backend reports a terminal error, JSON mode returns an error
envelope and exits `1`:

```json
{"status":"error","backend":"claude","session":null,
 "session_id":"...","reason":"not_logged_in",
 "message":"Not logged in · Please run /login"}
```

## Streaming JSONL

`--stream` writes one JSON object per line to stdout and flushes every line so
a parent process can consume progress while `kage` is still running:

```bash
$ kage claude --stream "run the tests and summarize failures"
{"status":"progress","backend":"claude","session":null,"session_id":"...","ts":1760000000.0,"event":"PreToolUse","tool":"Bash","summary":"pytest"}
{"status":"progress","backend":"claude","session":null,"session_id":"...","ts":1760000001.0,"event":"PostToolUse","tool":"Bash","summary":"pytest"}
{"status":"done","backend":"claude","session":null,"session_id":"...","response":"All tests passed."}
```

Error envelopes in stream mode use the same shape as `--json` errors, but are
also flushed immediately.

## Library use

```python
from kage import Session, get_backend

# Three ways to construct a Session:
named = Session.named("work", get_backend("claude"))
by_id = Session.by_id("4c8b...-69b6", get_backend("claude"))
oneshot = Session.ephemeral(get_backend("claude"))

with named as s:
    print(s.send("hello").text)
```

## Supported CLI

`kage` currently targets Claude Code. The backend abstraction is intentionally
small, but the product is focused on subscription-backed Claude Code
automation.

## How it works

1. `kage` spawns the target CLI inside a detached tmux session, pinned to a
   specific session UUID via `--session-id` (new) or `--resume` (existing).
2. Your message is loaded into a tmux paste buffer and pasted into the pane,
   then `Enter` is sent as a separate keystroke (sending them together is
   racy for some TUIs).
3. `kage` polls the rendered pane and watches for backend-specific markers
   (e.g. Claude's `✻ <verb> for Ns`) to know when the response is complete.
   In `--stream` mode it also starts Claude with a small hook settings file,
   tails the hook event log, and flushes those progress events as JSONL.
4. The final response is parsed out of the rendered TUI, or from Claude's
   transcript when hook-driven streaming is active, and printed to stdout.
5. Interactive menus are treated as automation failures with exit code 10.
   kage does not ask the caller to pick from TUI choices.

## Caveats

Things to know before you wire kage into anything important.

- **Auto-memory persists across `session clear`.** `clear` rotates the
  conversation UUID, but Claude's auto-memory files (facts saved across
  sessions) are independent. A "cleared" session will still recall things
  Claude wrote to memory in a previous turn. For full isolation you need to
  manually clear memory.
- **kage parses the rendered TUI.** If the AI CLI changes its prompt
  marker, done marker, tool-call format, or spinner style, kage's
  extraction may break until it's updated. Pin a known-good version of the
  underlying CLI in production environments. `tests/<backend>/fixtures/`
  contains real pane snapshots used as regression anchors — when an
  upstream change breaks something, capture the new pane, drop it in as
  a fixture, and fix parsing until tests pass.
- **Default response formatting comes from the rendered TUI.** In ordinary
  text and `--json` mode, triple-backtick fences, bold/italic markers, and
  link syntax may be gone by the time kage sees the text. `--stream` can use
  Claude's transcript for the final response when hooks are active, but if
  your caller needs guaranteed verbatim model output, you want the API.
- **Very long single lines wrap.** The tmux pane is 500 columns wide.
  Anything longer wraps in Claude's renderer and round-trips as multiple
  lines. Most prose and code fits; pathological one-liners (single huge
  JSON blobs, base64, etc.) won't.
- **Don't type into a kage-managed tmux session mid-call.** You can attach
  with `tmux attach -t kage_<backend>_<slug>` to observe, but if you send
  keys while kage has a request in flight, the next response extraction
  will be wrong. Take turns.
- **Tool calls are summarized, not replayed.** In default and `--json` mode,
  callers only receive the final assistant response. In `--stream` mode,
  callers receive hook-level tool names and short summaries, not full tool
  inputs, outputs, or partial assistant text.
- **`kage session show` needs the tmux pane to be running.** If you have
  only a state record (e.g. just after a reboot), run a regular
  `kage claude --session=NAME "..."` first to revive the pane.
- **tmux session name collisions are not auto-resolved.** kage uses
  `kage_<backend>_<slug>` as the tmux name. If you have another tmux
  session at that exact name, kage refuses to start. Pick a different
  `--session` name or kill the existing tmux session.
- **First call after `tmux kill-server` is slow.** tmux has to spawn the
  server and start the CLI from cold. Expect 3 to 5 seconds of warm-up
  before the first response.

## What this isn't

- Not a wrapper around the API. `kage` only drives the interactive CLI.
- Not a credential broker. Your subscription auth stays wherever the
  underlying CLI already keeps it.
- Not magic. If the TUI changes its output format, `kage` may need
  updating.

## License

MIT
