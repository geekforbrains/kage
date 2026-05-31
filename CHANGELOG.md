# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-05-31

First tagged release.

### Added

- `kage claude "<prompt>"` runs a one-shot, `claude -p`-style request by driving Claude Code's interactive TUI in a detached tmux session, so scripted automation runs on a Claude subscription instead of `claude -p`'s API billing. Response goes to stdout, errors to stderr, exit code reflects success
- Two session-persistence models: kage-managed named sessions (`--session NAME`, UUID tracked in `~/.local/state/kage/sessions.json`) and caller-managed sessions (`--session-id UUID`). Both survive reboots
- `--stream` emits newline-delimited JSON progress events (hook-driven `PreToolUse`/`PostToolUse`) followed by a final `done` envelope; `--json` emits a single structured envelope
- Autonomous mode is the default: interaction tools (`AskUserQuestion`, `EnterPlanMode`, `ExitPlanMode`) are disallowed and a system prompt tells Claude to make reasonable assumptions rather than pause for TUI input. AskUserQuestion's confirmation step is auto-submitted, and multi-question prompts are answered one at a time
- Session lifecycle controls: `kage session list|kill|rm|show|compact|clear|prune`, with `--session-id` accepted for live-pane operations. `prune` reaps idle panes and orphaned hook/event artifacts (`--dry-run`, `--older-than`)
- Request options `--timeout`, `--model`, `--effort`, `--restart` (fresh pane for the same conversation UUID), and `--stop-on-signal` (tear down the pane on SIGINT/SIGTERM)
- `ENSO_ORIGIN_*` environment passthrough into driven sessions
- Scripting exit-code contract: `0` success, `1` error, `2` usage, `10` interaction required, `11` session busy, `124` timeout
- `kage doctor` environment check and a `Session` / `get_backend` library API

### Fixed

- Ephemeral one-shot sessions always tear down their tmux pane on interruption, so an aborted run never leaves an orphaned pane behind
- Pre-tool narration text is no longer mistaken for the final response; the answer is taken from the terminal transcript message
- Session listing and ephemeral cleanup hardened against stale and orphaned state
