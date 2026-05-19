"""kage command-line interface."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from . import __version__
from . import tmux as tmuxlib
from .backends import get_backend, list_backends
from .session import (
    BackendFailure,
    MenuPending,
    Session,
    SessionBusy,
    State,
    known_records,
    list_sessions,
)
from . import state as state_mod

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_MENU = 10
EXIT_BUSY = 11
EXIT_TIMEOUT = 124


# --- input/output helpers ---

def _resolve_message(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if getattr(args, "message", None):
        parts.append(args.message)
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
        except Exception:
            stdin_data = ""
        if stdin_data.strip():
            parts.append(stdin_data.rstrip())
    if not parts:
        print("error: no message (pass as argument or pipe via stdin)", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    return "\n\n".join(parts)


def _output_mode(args: argparse.Namespace) -> str:
    """Resolve --output-format and legacy --json into a single mode."""
    fmt = getattr(args, "output_format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "text"


def _should_stream_text(args: argparse.Namespace, mode: str) -> bool:
    if mode != "text":
        return False
    if getattr(args, "no_stream", False):
        return False
    return sys.stdout.isatty()


def _emit_response(text: str, *, mode: str, backend: str, session: str | None, session_id: str | None) -> None:
    if mode == "json":
        print(json.dumps({
            "status": "done",
            "backend": backend,
            "session": session,
            "session_id": session_id,
            "response": text,
        }))
    else:
        print(text)


def _emit_menu(menu, *, mode: str, backend: str, session: str | None, session_id: str | None) -> None:
    if mode == "json":
        print(json.dumps({
            "status": "menu",
            "backend": backend,
            "session": session,
            "session_id": session_id,
            "menu": {
                "question": menu.question if menu else None,
                "options": menu.options if menu else [],
            },
        }))
        return
    print(f"MENU: {menu.question if menu else '(unparseable)'}", file=sys.stderr)
    for i, opt in enumerate(menu.options if menu else [], start=1):
        print(f"  {i}. {opt}", file=sys.stderr)
    print(
        f"answer with: kage session choose {session or '<name>'} <number>",
        file=sys.stderr,
    )


def _emit_event(event: dict) -> None:
    print(json.dumps(event), flush=True)


def _emit_error(
    *,
    mode: str,
    backend: str,
    session: str | None,
    session_id: str | None,
    reason: str,
    message: str,
) -> None:
    if mode == "json":
        print(json.dumps({
            "status": "error",
            "backend": backend,
            "session": session,
            "session_id": session_id,
            "reason": reason,
            "message": message,
        }))
    else:
        print(f"error: {message}", file=sys.stderr)


# --- session resolution ---

def _build_session(args: argparse.Namespace, backend_name: str) -> tuple[Session, bool]:
    """Return (session, cleanup_after) given parsed args."""
    backend = get_backend(backend_name)
    if args.session and args.session_id:
        print("error: --session and --session-id are mutually exclusive", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    if args.session_id:
        return Session.by_id(args.session_id, backend), False
    if args.session:
        return Session.named(
            args.session, backend,
            bare=args.bare,
            model=args.model,
            effort=args.effort,
            system_prompt=args.system_prompt,
        ), False
    return Session.ephemeral(backend), True


# --- backend dispatch (the primary command form) ---

def cmd_backend(args: argparse.Namespace, backend_name: str) -> int:
    message = _resolve_message(args)
    mode = _output_mode(args)
    sess, cleanup_after = _build_session(args, backend_name)

    started_now = False
    if not sess.exists():
        try:
            sess.start(
                system_prompt=args.system_prompt,
                bare=args.bare,
                model=args.model,
                effort=args.effort,
            )
            started_now = True
        except Exception as e:
            print(f"error: failed to start {backend_name}: {e}", file=sys.stderr)
            return EXIT_ERROR
    else:
        for flag in ("system_prompt", "model", "effort"):
            if getattr(args, flag):
                print(
                    f"warning: --{flag.replace('_', '-')} ignored (session already running)",
                    file=sys.stderr,
                )

    try:
        if mode == "stream-json":
            return _run_stream_json(sess, message, args)
        if _should_stream_text(args, mode):
            return _run_stream_text(sess, message, args, backend_name)
        return _run_send(sess, message, args, backend_name, mode)
    finally:
        if cleanup_after:
            sess.stop()


def _run_send(sess: Session, message: str, args, backend_name: str, mode: str) -> int:
    try:
        result = sess.send(
            message,
            timeout=args.timeout,
            wait_if_busy=not args.no_wait,
        )
    except SessionBusy as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_BUSY
    except BackendFailure as e:
        _emit_error(
            mode=mode,
            backend=backend_name,
            session=args.session,
            session_id=sess.session_id,
            reason=e.error.reason,
            message=e.error.message,
        )
        return EXIT_ERROR
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    if result.state == State.MENU:
        _emit_menu(result.menu, mode=mode, backend=backend_name,
                   session=args.session, session_id=sess.session_id)
        return EXIT_MENU
    _emit_response(result.text, mode=mode, backend=backend_name,
                   session=args.session, session_id=sess.session_id)
    return EXIT_OK


def _run_stream_text(sess: Session, message: str, args, backend_name: str) -> int:
    try:
        for chunk in sess.stream(
            message,
            timeout=args.timeout,
            wait_if_busy=not args.no_wait,
        ):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        print()
        return EXIT_OK
    except SessionBusy as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_BUSY
    except BackendFailure as e:
        print(f"\nerror: {e.error.message}", file=sys.stderr)
        return EXIT_ERROR
    except MenuPending as e:
        print()
        _emit_menu(e.menu, mode="text", backend=backend_name,
                   session=args.session, session_id=sess.session_id)
        return EXIT_MENU
    except TimeoutError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return EXIT_TIMEOUT


def _run_stream_json(sess: Session, message: str, args) -> int:
    try:
        last_type = None
        for ev in sess.stream_events(
            message,
            timeout=args.timeout,
            wait_if_busy=not args.no_wait,
        ):
            _emit_event(ev)
            last_type = ev.get("type")
        if last_type == "menu":
            return EXIT_MENU
        return EXIT_OK
    except SessionBusy as e:
        _emit_event({"type": "error", "message": str(e), "reason": "busy"})
        return EXIT_BUSY
    except BackendFailure as e:
        _emit_event({
            "type": "error",
            "message": e.error.message,
            "reason": e.error.reason,
        })
        return EXIT_ERROR
    except TimeoutError as e:
        _emit_event({"type": "error", "message": str(e), "reason": "timeout"})
        return EXIT_TIMEOUT


# --- session subcommands ---

def cmd_session_list(args: argparse.Namespace) -> int:
    running = {(b, n) for b, n in list_sessions()}
    records = {r.name: r for r in known_records()}

    rows: list[dict] = []
    for rec in records.values():
        is_running = any(
            b == rec.backend and slug_matches(rec.name, slug)
            for b, slug in running
        )
        rows.append({
            "name": rec.name,
            "backend": rec.backend,
            "session_id": rec.session_id,
            "running": is_running,
            "last_used_at": rec.last_used_at,
        })
    for backend, slug in running:
        if not any(r["backend"] == backend and slug_matches(r["name"], slug) for r in rows):
            rows.append({
                "name": None,
                "backend": backend,
                "session_id": None,
                "running": True,
                "slug": slug,
            })

    if args.json:
        print(json.dumps(rows))
        return EXIT_OK
    if not rows:
        print("(no sessions)")
        return EXIT_OK
    for r in rows:
        running_tag = "running" if r["running"] else "stopped"
        name = r["name"] or f"<anonymous:{r.get('slug','?')}>"
        sid = (r["session_id"] or "")[:8]
        print(f"{r['backend']}\t{name}\t{sid}\t{running_tag}")
    return EXIT_OK


def slug_matches(name: str, slug: str) -> bool:
    from .session import _sanitize  # local import to avoid circular
    return _sanitize(name) == slug


def _find_session(name: str) -> tuple[str, str, str | None]:
    """Return (backend, name, session_id) for a named session record.

    Falls back to running tmux session if no record (anonymous sessions).
    """
    rec = state_mod.get(name)
    if rec:
        return rec.backend, name, rec.session_id
    for backend, slug in list_sessions():
        if slug == name or slug_matches(name, slug):
            return backend, name, None
    raise KeyError(name)


def cmd_session_kill(args: argparse.Namespace) -> int:
    try:
        backend_name, name, _sid = _find_session(args.name)
    except KeyError:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    sess = Session.named(name, get_backend(backend_name)) if state_mod.get(name) \
        else Session(backend=get_backend(backend_name), slug=name)
    if sess.stop():
        print(f"stopped: {backend_name}/{name}")
        return EXIT_OK
    print(f"(no running tmux pane for {backend_name}/{name}; record preserved)")
    return EXIT_OK


def cmd_session_rm(args: argparse.Namespace) -> int:
    """Stop tmux AND forget the persisted record."""
    rec = state_mod.get(args.name)
    if not rec:
        try:
            _find_session(args.name)
        except KeyError:
            print(f"error: no session named {args.name!r}", file=sys.stderr)
            return EXIT_ERROR
    if rec:
        sess = Session.named(args.name, get_backend(rec.backend))
        sess.stop()
        sess.forget()
        print(f"removed: {rec.backend}/{args.name}")
    else:
        backend, name, _ = _find_session(args.name)
        sess = Session(backend=get_backend(backend), slug=name)
        sess.stop()
        print(f"stopped anonymous: {backend}/{name}")
    return EXIT_OK


def cmd_session_show(args: argparse.Namespace) -> int:
    try:
        backend_name, name, _sid = _find_session(args.name)
    except KeyError:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    sess = Session.named(name, get_backend(backend_name)) if state_mod.get(name) \
        else Session(backend=get_backend(backend_name), slug=name)
    if not sess.exists():
        print(f"(session {name!r} has no running tmux pane)", file=sys.stderr)
        return EXIT_ERROR
    sys.stdout.write(sess.capture())
    return EXIT_OK


def cmd_session_menu(args: argparse.Namespace) -> int:
    try:
        backend_name, name, _sid = _find_session(args.name)
    except KeyError:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    sess = Session.named(name, get_backend(backend_name)) if state_mod.get(name) \
        else Session(backend=get_backend(backend_name), slug=name)
    if not sess.exists():
        print(f"(session {name!r} has no running tmux pane)", file=sys.stderr)
        return EXIT_ERROR
    pane = sess.capture()
    if not sess.backend.is_menu(pane):
        if args.json:
            print(json.dumps({"status": "no_menu"}))
        else:
            print("(no menu pending)")
        return EXIT_OK
    menu = sess.backend.extract_menu(pane)
    _emit_menu(menu, mode="json" if args.json else "text",
               backend=backend_name, session=name, session_id=sess.session_id)
    return EXIT_MENU


def cmd_session_choose(args: argparse.Namespace) -> int:
    try:
        backend_name, name, _sid = _find_session(args.name)
    except KeyError:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    sess = Session.named(name, get_backend(backend_name)) if state_mod.get(name) \
        else Session(backend=get_backend(backend_name), slug=name)
    try:
        choice: int | str = int(args.choice)
    except ValueError:
        choice = args.choice
    try:
        result = sess.respond_to_menu(choice)
    except BackendFailure as e:
        _emit_error(
            mode="json" if args.json else "text",
            backend=backend_name,
            session=name,
            session_id=sess.session_id,
            reason=e.error.reason,
            message=e.error.message,
        )
        return EXIT_ERROR
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    mode = "json" if args.json else "text"
    if result.state == State.MENU:
        _emit_menu(result.menu, mode=mode, backend=backend_name,
                   session=name, session_id=sess.session_id)
        return EXIT_MENU
    _emit_response(result.text, mode=mode, backend=backend_name,
                   session=name, session_id=sess.session_id)
    return EXIT_OK


def cmd_session_compact(args: argparse.Namespace) -> int:
    rec = state_mod.get(args.name)
    if not rec:
        print(f"error: no named session {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    sess = Session.named(args.name, get_backend(rec.backend))
    if not sess.exists():
        print(f"error: session {args.name!r} is not running. Start it first.", file=sys.stderr)
        return EXIT_ERROR
    try:
        result = sess.slash("/compact", timeout=args.timeout)
    except BackendFailure as e:
        _emit_error(
            mode="json" if args.json else "text",
            backend=rec.backend,
            session=args.name,
            session_id=sess.session_id,
            reason=e.error.reason,
            message=e.error.message,
        )
        return EXIT_ERROR
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    if args.json:
        print(json.dumps({"status": "compacted", "session": args.name, "summary": result.text}))
    else:
        print(f"compacted: {args.name}")
        if result.text:
            print(result.text)
    return EXIT_OK


def cmd_session_clear(args: argparse.Namespace) -> int:
    rec = state_mod.get(args.name)
    if not rec:
        print(f"error: no named session {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    sess = Session.named(args.name, get_backend(rec.backend))
    sess.reset()
    if args.json:
        print(json.dumps({"status": "cleared", "session": args.name,
                          "session_id": sess.session_id}))
    else:
        print(f"cleared: {args.name} (new session_id={sess.session_id})")
    return EXIT_OK


# --- meta commands ---

def cmd_backends(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(list_backends()))
    else:
        for b in list_backends():
            print(b)
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    tmux_path = shutil.which("tmux")
    if tmux_path:
        try:
            ver = subprocess.run(
                ["tmux", "-V"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            checks.append(("tmux", True, ver))
        except Exception as e:
            checks.append(("tmux", False, f"error: {e}"))
    else:
        checks.append(("tmux", False, "not installed"))

    for backend_name in list_backends():
        backend = get_backend(backend_name)
        cmd = backend.start_command()[0]
        path = shutil.which(cmd)
        if path:
            checks.append((backend_name, True, f"{cmd} at {path}"))
        else:
            checks.append((backend_name, False, f"{cmd} not on PATH"))

    if args.json:
        print(json.dumps([
            {"name": n, "ok": ok, "detail": d} for n, ok, d in checks
        ]))
    else:
        for name, ok, detail in checks:
            mark = "ok " if ok else "FAIL"
            print(f"[{mark}] {name}: {detail}")

    return EXIT_OK if all(ok for _, ok, _ in checks) else EXIT_ERROR


# --- argparse wiring ---

def _add_backend_subparser(sub, backend_name: str) -> None:
    p = sub.add_parser(
        backend_name,
        help=f"send a message to {backend_name}",
        description=(
            f"Send a message to {backend_name} and print the response. "
            "Without --session or --session-id, an ephemeral one-shot is used. "
            "Pass --session NAME for a kage-managed persistent session, or "
            "--session-id UUID to drive a specific underlying conversation."
        ),
    )
    p.add_argument("message", nargs="?", default=None,
                   help="message (or read from stdin)")
    p.add_argument("--session", "-s", default=None,
                   help="reuse a named persistent session (kage-managed UUID)")
    p.add_argument("--session-id", default=None,
                   help="bind to a specific conversation UUID (caller-managed)")
    p.add_argument("--timeout", "-t", type=float, default=120.0,
                   help="seconds to wait for response (default 120)")
    p.add_argument("--system-prompt", default=None,
                   help="appended to backend system prompt (session-start only)")
    p.add_argument("--bare", action="store_true",
                   help="pass --bare to the backend: minimal mode, no auto-memory, no CLAUDE.md")
    p.add_argument("--model", default=None,
                   help="model alias to pass through (e.g. opus, sonnet)")
    p.add_argument("--effort", default=None,
                   help="effort level to pass through (low, medium, high, xhigh, max)")
    p.add_argument("--output-format", choices=["text", "json", "stream-json"],
                   default=None, help="output format (default: text)")
    p.add_argument("--json", action="store_true",
                   help="shorthand for --output-format=json")
    p.add_argument("--no-stream", action="store_true",
                   help="never stream text, even to a tty")
    p.add_argument("--no-wait", action="store_true",
                   help="exit with code 11 instead of waiting if session is busy")
    p.set_defaults(func=lambda a, bn=backend_name: cmd_backend(a, bn))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kage",
        description="Scriptable bridge for interactive AI CLIs.",
    )
    parser.add_argument("--version", "-V", action="version",
                        version=f"kage {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    for backend_name in list_backends():
        _add_backend_subparser(sub, backend_name)

    sp = sub.add_parser("session", help="manage long-lived sessions")
    sess_sub = sp.add_subparsers(dest="session_cmd", required=True, metavar="ACTION")

    p = sess_sub.add_parser("list", help="list known and running sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_list)

    p = sess_sub.add_parser("kill", help="stop the tmux pane (keep the record)")
    p.add_argument("name")
    p.set_defaults(func=cmd_session_kill)

    p = sess_sub.add_parser("rm", help="stop and forget a named session")
    p.add_argument("name")
    p.set_defaults(func=cmd_session_rm)

    p = sess_sub.add_parser("show", help="dump raw pane (debug)")
    p.add_argument("name")
    p.set_defaults(func=cmd_session_show)

    p = sess_sub.add_parser("menu", help="show pending menu, if any")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_menu)

    p = sess_sub.add_parser("choose", help="answer a pending menu by number or text")
    p.add_argument("name")
    p.add_argument("choice")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_choose)

    p = sess_sub.add_parser("compact", help="run /compact on a running named session")
    p.add_argument("name")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_compact)

    p = sess_sub.add_parser(
        "clear",
        help="clear context: kill tmux and rotate to a fresh underlying UUID",
    )
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_clear)

    p = sub.add_parser("backends", help="list available backends")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backends)

    p = sub.add_parser("doctor", help="check environment and dependencies")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except tmuxlib.TmuxError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
