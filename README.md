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

`kage` waits for Claude to finish and prints the final response once. This is
intentional: the public contract is modeled after `claude -p`-style output,
not Claude's interactive stream. The implementation still runs through the
TUI; it does not invoke Claude Code's `-p`/`--print` mode.

## Calling from scripts

`kage` is built to be called from automation. The contract is stable:
`--json` or `--output-format` for structured output, exit codes for status,
stdin for piped input.

```python
import subprocess, json

result = subprocess.run(
    ["kage", "claude", "--json",
     "--autonomous"],
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

`kage` does not expose tool-use events or partial text deltas. It drives the
interactive CLI internally and returns the final assistant response.

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
- `--autonomous` -- disable Claude Code tools that can pause for TUI input
  (`AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode`) and append instructions
  to make assumptions instead of asking; session start only
- `--model NAME` -- model alias to pass through (e.g. opus, sonnet)
- `--effort LEVEL` -- effort level to pass through (low/medium/high/xhigh/max)
- `--bare` -- pass `--bare` to the backend. **Caution:** for Claude Code
  this forces API-key mode and skips your subscription auth. See Caveats.
- `--output-format {text,json}` -- output format (default text)
- `--json` -- shorthand for `--output-format=json`
- `--no-wait` -- exit with code 11 instead of waiting if the session is
  mid-response from a previous call

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Error (session crashed, backend missing, not logged in, empty response, etc.) |
| 2    | Usage error |
| 10   | Menu pending, awaiting input |
| 11   | Session busy and `--no-wait` was set |
| 124  | Timeout (no response within `--timeout` seconds) |

## JSON output

```bash
$ kage claude --json "what is 2+2"
{"status":"done","backend":"claude","session":null,"session_id":"...","response":"4"}
```

When the TUI is waiting on a menu, you get:

```json
{"status":"menu","backend":"claude","session":"work",
 "session_id":"...",
 "menu":{"question":"Do you want to create hello.txt?","options":["Yes","Yes, allow all edits during this session (shift+tab)","No"]}}
```

The exit code is `10`. Respond with `kage session choose <name> <number>`.
The same envelope is returned for plan-approval menus, tool-permission
prompts (when `--dangerously-skip-permissions` is off — kage passes that
flag by default), Claude's `AskUserQuestion` clarifications, and the
first-run trust-folder dialog. `question` is whatever Claude wrote;
match on `options` if you need to dispatch on intent.

When the backend reports a terminal error, JSON mode returns an error
envelope and exits `1`:

```json
{"status":"error","backend":"claude","session":null,
 "session_id":"...","reason":"not_logged_in",
 "message":"Not logged in · Please run /login"}
```

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
   (e.g. Claude's `✻ <verb> for Ns`) to know when the response is complete.
4. The final response is parsed out of the rendered TUI and printed to
   stdout.
5. Interactive menus (plan approval, etc.) are returned as a structured
   envelope with exit code 10, so callers can decide how to respond.

## Caveats

Things to know before you wire kage into anything important.

- **`--bare` forces API-key mode for Claude Code.** It skips keychain reads,
  so your subscription auth is ignored. Without `ANTHROPIC_API_KEY` set,
  the session will fail; with it set, you'll be charged via the API. Don't
  pass `--bare` unless you have an API key and want to use it.
- **Auto-memory persists across `session clear`.** `clear` rotates the
  conversation UUID, but Claude's auto-memory files (facts saved across
  sessions) are independent. A "cleared" session will still recall things
  Claude wrote to memory in a previous turn. For full isolation you need
  API-key mode (`--bare`) or to manually clear memory.
- **kage parses the rendered TUI.** If the AI CLI changes its prompt
  marker, done marker, tool-call format, or spinner style, kage's
  extraction may break until it's updated. Pin a known-good version of the
  underlying CLI in production environments. `tests/<backend>/fixtures/`
  contains real pane snapshots used as regression anchors — when an
  upstream change breaks something, capture the new pane, drop it in as
  a fixture, and fix parsing until tests pass.
- **Markdown and code-fence formatting is lost.** kage reads the
  *rendered* pane, so triple-backtick fences, bold/italic markers, and
  link syntax are gone by the time we see the text — only the visible
  characters survive. If your caller needs verbatim model output, you
  want the API, not kage.
- **Very long single lines wrap.** The tmux pane is 500 columns wide.
  Anything longer wraps in Claude's renderer and round-trips as multiple
  lines. Most prose and code fits; pathological one-liners (single huge
  JSON blobs, base64, etc.) won't.
- **Don't type into a kage-managed tmux session mid-call.** You can attach
  with `tmux attach -t kage_<backend>_<slug>` to observe, but if you send
  keys while kage has a request in flight, the next response extraction
  will be wrong. Take turns.
- **Tool calls are not part of the public contract.** `kage` strips Claude's
  collapsed tool chrome when it can, but callers only receive the final
  assistant response.
- **Some menu choices return DONE with empty text.** When you pick an
  option that dismisses the menu without generating a new model response
  (e.g. plan mode's "Tell Claude what to change"), kage returns exit 0
  with `response: ""`. That's a successful action, not a failure — the
  next `kage claude --session=...` call resumes normally.
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
