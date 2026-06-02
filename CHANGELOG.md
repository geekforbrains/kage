# Changelog

All notable changes to this project will be documented in this file.

## [0.2.1] - 2026-06-02

### Fixed

- Turn completion was missed for any turn running longer than 60 seconds. The done-marker pattern only matched whole-second elapsed times (`✻ Verb for 5s`), but Claude Code renders the summary in compound units past a minute (`✻ Verb for 1m 16s`). In non-stream mode — where that marker is the sole completion signal — a long turn never registered as done and blocked until the caller's timeout. The duration token now accepts `h`/`m`/`s` units
- After a `--restart`/resume, the previous turn's response could be returned in place of the new one (a session crossover). `claude --resume` re-renders the entire prior conversation into the pane, so a stale done-marker could trip completion before the new answer existed, and the pane scrape then returned the old response. In stream mode the session transcript is now the sole source of truth: completion is accepted only once a fresh terminal assistant message lands, and the poisoned pane is never used as a fallback

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
