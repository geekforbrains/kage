# Claude pane fixtures

Each `.txt` file is a real `tmux capture-pane` snapshot of a Claude Code session in a specific state. Tests in `test_backend.py` load these and assert that `ClaudeBackend` interprets them correctly.

When Claude's TUI changes (e.g. a new spinner glyph, a renamed menu header, a different option layout), the relevant tests will fail. The recovery flow:

1. Reproduce the new state against `kage` and capture the pane:
   ```
   tmux capture-pane -t kage_claude_<slug> -p -J -S - > new_capture.txt
   ```
2. Diff against the existing fixture to confirm the UI change.
3. Update the fixture (or add a new one for a new scenario).
4. Update parsing in `src/kage/backends/claude.py` if needed.

**Captured against:** Claude Code 2.1.141, kage pane width 500.
