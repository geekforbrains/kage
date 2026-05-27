"""Claude Code backend."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .base import Backend, BackendError, Menu

PROMPT_RE = re.compile(r"^❯(?:[\xa0 ]|$)")
DONE_RE = re.compile(r"^✻ \S+ for \d+s")
MENU_OPTION_RE = re.compile(r"^\s*(?:❯[\xa0 ])?\s*(\d+)\.\s+(.+?)\s*$")
MENU_SELECTOR_RE = re.compile(r"^\s*❯[\xa0 ]\s*\d+\.\s+")
TOOL_CALL_RE = re.compile(
    r"^⏺ ([A-Z][A-Za-z0-9_]*(?:\s[A-Z][A-Za-z0-9_]*)*)\((.*?)\)\s*$"
)
# Active-work spinner: line starts with one of Claude's animated bullets, a
# verb ending in `…`. Seen variants: `· Percolating…`, `✻ Puttering…`,
# `✢ Percolating…`. Update if Claude adds new bullet glyphs.
SPINNER_RE = re.compile(r"^\s*[·✴✦✶✻✼✱✸✵✢✺✷*]\s+\S+…")
RESPONSE_MARKER = "⏺ "
COMPACT_DONE_RE = re.compile(r"^\s*⎿\s+Compacted\b")
LOGIN_ERROR_RE = re.compile(r"\bNot logged in\b|\bPlease run /login\b|\bRun /login\b")
# AskUserQuestion's confirmation/review step shown after options are picked.
SUBMIT_CONFIRM_RE = re.compile(r"submit your answers|ready to submit", re.IGNORECASE)
# Tab bar of the multi-question AskUserQuestion UI, e.g.
# `←  ☒ Indentation  ☐ Color  ✔ Submit  →`. The checkbox glyphs + a Submit tab
# distinguish it from ordinary single menus.
MULTI_Q_TABBAR_RE = re.compile(r"[☐☒].*\bSubmit\b")
GENERIC_ERROR_RE = re.compile(
    r"\b(?:API Error|Authentication error|Unauthorized|Invalid API key|"
    r"Credit balance|rate limit|permission denied)\b",
    re.IGNORECASE,
)
# Known static menu headers — still surfaced verbatim when seen.
MENU_HEADERS = (
    "Ready to code?",
    "Enable auto mode?",
    "Would you like to proceed?",
    "Trust the files in this folder?",
)
# Lines below the option block we treat as boundary markers (not part of the menu).
_MENU_HINT_PREFIXES = (
    "Esc ", "Enter ", "Tab ", "shift+tab", "ctrl-", "ctrl+", "↑", "←",
)


def _has_prompt_content(line: str) -> bool:
    """Detect a `❯ ...` line where the prompt actually contains a message."""
    if not PROMPT_RE.match(line):
        return False
    return bool(line[2:].strip())


def _is_divider(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return set(s) <= set("─╌-═━")


def _menu_continuation(line: str) -> bool:
    """True if `line` plausibly continues an option block (option / description / blank)."""
    if not line.strip():
        return True
    if MENU_OPTION_RE.match(line):
        return True
    # Indented description line (AskUserQuestion uses 4+ leading spaces).
    if line.startswith("    "):
        return True
    return False


class ClaudeBackend(Backend):
    name = "claude"

    def start_command(
        self,
        *,
        session_id: str | None = None,
        display_name: str | None = None,
        system_prompt: str | None = None,
        bare: bool = False,
        model: str | None = None,
        effort: str | None = None,
        settings: str | None = None,
    ) -> list[str]:
        # --dangerously-skip-permissions is required, not optional: without it
        # every tool call raises a permission menu that would block a scripted,
        # non-interactive caller. kage is built to drive trusted automation, so
        # it always bypasses permission prompts.
        cmd = ["claude", "--dangerously-skip-permissions"]
        if settings:
            cmd += ["--settings", settings]
        if session_id:
            if _conversation_file(session_id).exists():
                cmd += ["--resume", session_id]
            else:
                cmd += ["--session-id", session_id]
        if display_name:
            cmd += ["--name", display_name]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if bare:
            cmd.append("--bare")
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def ready_marker(self, pane: str) -> bool:
        return any(PROMPT_RE.match(l) for l in pane.splitlines())

    def is_done(self, pane: str) -> bool:
        return any(DONE_RE.match(l) for l in pane.splitlines())

    def done_marker_count(self, pane: str) -> int:
        return sum(1 for l in pane.splitlines() if DONE_RE.match(l))

    def is_busy(self, pane: str) -> bool:
        """Active-work signal: a spinner is rendered and no menu is pending.

        Previously this counted `❯ <content>` lines against `✻ Verb for Ns` lines,
        which broke whenever (a) a verb contained Unicode (`Sautéed`), (b) plan
        mode rendered the prompt twice and never emitted a `for Ns` marker, or
        (c) the menu was up. Spinner-based detection sidesteps all three.
        """
        if self.is_menu(pane):
            return False
        return any(SPINNER_RE.search(l) for l in pane.splitlines())

    def is_menu(self, pane: str) -> bool:
        """A menu is on screen if any option line carries the `❯` selector."""
        return any(MENU_SELECTOR_RE.match(l) for l in pane.splitlines())

    def extract_response(self, pane: str) -> str:
        lines = pane.splitlines()
        last_done = -1
        for i, l in enumerate(lines):
            if DONE_RE.match(l):
                last_done = i
        if last_done < 0:
            return ""
        last_prompt = -1
        for i in range(last_done - 1, -1, -1):
            if PROMPT_RE.match(lines[i]):
                last_prompt = i
                break
        if last_prompt < 0:
            return ""
        response_start = -1
        for i in range(last_prompt + 1, last_done):
            if lines[i].startswith(RESPONSE_MARKER):
                response_start = i
                break
        if response_start < 0:
            return ""
        body = lines[response_start : last_done]
        cleaned = []
        for l in body:
            if _is_tool_chrome(l):
                continue
            if l.startswith(RESPONSE_MARKER):
                l = l[len(RESPONSE_MARKER):]
            elif l.startswith("  "):
                l = l[2:]
            cleaned.append(l)
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return "\n".join(cleaned)

    def extract_menu(self, pane: str) -> Menu | None:
        """Return the on-screen menu by walking out from the `❯` selector.

        We bound option scanning to the contiguous block around the selector so
        that numbered lines in plan-body content (e.g. `1. Create hello.py at
        ...`) don't get picked up as options.
        """
        lines = pane.splitlines()
        selector_idx = -1
        for i, l in enumerate(lines):
            if MENU_SELECTOR_RE.match(l):
                selector_idx = i
                break
        if selector_idx < 0:
            return None

        start = selector_idx
        while start > 0 and MENU_OPTION_RE.match(lines[start - 1]):
            start -= 1

        end = selector_idx
        while end + 1 < len(lines):
            nxt = lines[end + 1]
            if _is_divider(nxt):
                break
            stripped = nxt.strip()
            if stripped and any(stripped.startswith(p) for p in _MENU_HINT_PREFIXES):
                break
            if _menu_continuation(nxt):
                end += 1
                continue
            break

        options: list[str] = []
        for i in range(start, end + 1):
            m = MENU_OPTION_RE.match(lines[i])
            if m:
                options.append(m.group(2))
        if not options:
            return None

        question = ""
        for i in range(start - 1, max(-1, start - 25), -1):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                continue
            # Stop at a previous user prompt; the menu's question can't sit
            # above it.
            if PROMPT_RE.match(line) and stripped != "❯":
                break
            for h in MENU_HEADERS:
                if h in stripped:
                    question = h
                    break
            if question:
                break
            if "?" in stripped:
                # Take the substring up to the first `?`. Handles questions
                # embedded in longer paragraphs (e.g. the trust dialog's
                # "Quick safety check: …trust? (Like…). If not, …first.").
                question = stripped[: stripped.index("?") + 1]
                break
            # Dividers, chips, and chrome lines don't qualify but shouldn't
            # stop the search — keep walking back until we find a question.

        return Menu(
            question=question or "Choose an option",
            options=options,
            raw=pane,
        )

    def is_multi_question(self, pane: str) -> bool:
        return any(MULTI_Q_TABBAR_RE.search(l) for l in pane.splitlines())

    def auto_submit_option(self, menu: Menu) -> int | None:
        """Auto-confirm AskUserQuestion's "Ready to submit your answers?" step.

        The caller already chose their answer(s); this review menu is pure
        ceremony, so pick the "Submit answers" option and let kage proceed to
        the real response instead of surfacing a second menu.
        """
        if not menu or not SUBMIT_CONFIRM_RE.search(menu.question or ""):
            return None
        for i, opt in enumerate(menu.options, start=1):
            if opt.strip().lower().startswith("submit"):
                return i
        return None

    def extract_error(self, pane: str) -> BackendError | None:
        """Detect Claude diagnostics for the latest submitted prompt."""
        lines = pane.splitlines()
        start = 0
        for i, line in enumerate(lines):
            if _has_prompt_content(line):
                start = i + 1

        for i in range(start, len(lines)):
            clean = _clean_diagnostic_line(lines[i])
            if not clean:
                continue
            if _is_diagnostic_line(lines[i]) and LOGIN_ERROR_RE.search(clean):
                return BackendError(
                    message=_collect_diagnostic(lines, i),
                    reason="not_logged_in",
                    raw=pane,
                )
            if _is_diagnostic_line(lines[i]) and GENERIC_ERROR_RE.search(clean):
                return BackendError(
                    message=_collect_diagnostic(lines, i),
                    reason="backend_error",
                    raw=pane,
                )
        return None


    def _assistant_texts(self, session_id: str) -> list[str]:
        """Text of each text-bearing assistant message, in transcript order."""
        path = _conversation_file(session_id)
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        out: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                out.append(joined)
        return out

    def final_response(self, session_id: str) -> str | None:
        """Last assistant text block(s) from the session's JSONL transcript.

        The transcript stores verbatim content blocks, so this avoids the
        rendered-pane losses (markdown, line-wrap, tool chrome). Returns the
        text of the last assistant message that contains any text block.
        """
        texts = self._assistant_texts(session_id)
        return texts[-1] if texts else None

    def transcript_text_count(self, session_id: str) -> int:
        """How many text-bearing assistant messages the transcript holds.

        Baselined before a turn so the caller can tell a freshly-flushed answer
        from a stale prior one: a `final_response` is only trustworthy once this
        count has advanced past its pre-submit value.
        """
        return len(self._assistant_texts(session_id))


def _conversation_file(session_id: str) -> Path:
    """Where Claude stores the JSONL log for a session, scoped to current cwd.

    Claude rewrites the cwd by replacing both `/` and `.` with `-`, so e.g.
    `/Users/me/.enso/workspace` becomes `-Users-me--enso-workspace`.
    """
    sanitized = os.getcwd().replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"


def _is_tool_chrome(line: str) -> bool:
    """Strip Claude's collapsed tool-use lines and TUI hints."""
    s = line.strip()
    if not s:
        return False
    if s.startswith("⎿ "):
        return True
    if s.endswith("(ctrl+o to expand)"):
        return True
    if TOOL_CALL_RE.match(line):
        return True
    return False


def _is_diagnostic_line(line: str) -> bool:
    s = line.strip().replace("\xa0", " ")
    return s.startswith(("⎿", "·"))


def _clean_diagnostic_line(line: str) -> str:
    s = line.strip().replace("\xa0", " ")
    for prefix in ("⎿", "·"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    return s


def _collect_diagnostic(lines: list[str], index: int) -> str:
    out = [_clean_diagnostic_line(lines[index])]
    for line in lines[index + 1:index + 5]:
        if not _is_diagnostic_line(line):
            break
        clean = _clean_diagnostic_line(line)
        if clean:
            out.append(clean)
    return "\n".join(out)
