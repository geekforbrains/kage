"""kage command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

from . import __version__
from .backends import get_backend, list_backends
from .session import (
    MenuPending,
    Session,
    State,
    list_sessions,
    parse_session_name,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_MENU = 10
EXIT_TIMEOUT = 124


def _resolve_message(args: argparse.Namespace) -> str:
    """Pull message from positional arg, stdin, or both."""
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


def _should_stream(args: argparse.Namespace) -> bool:
    if getattr(args, "json", False):
        return False
    if getattr(args, "no_stream", False):
        return False
    return sys.stdout.isatty()


def _emit_response(text: str, *, as_json: bool, backend: str, session: str | None) -> None:
    if as_json:
        print(json.dumps({
            "status": "done",
            "backend": backend,
            "session": session,
            "response": text,
        }))
    else:
        print(text)


def _emit_menu(menu, *, as_json: bool, backend: str, session: str | None) -> None:
    if as_json:
        print(json.dumps({
            "status": "menu",
            "backend": backend,
            "session": session,
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


def _ephemeral_name() -> str:
    return f"oneshot-{os.getpid()}-{uuid.uuid4().hex[:6]}"


# --- backend dispatch (the primary command form) ---

def cmd_backend(args: argparse.Namespace, backend_name: str) -> int:
    message = _resolve_message(args)
    is_oneshot = args.session is None
    session_name = args.session or _ephemeral_name()

    backend = get_backend(backend_name)
    sess = Session(name=session_name, backend=backend)

    cleanup_after = False
    if not sess.exists():
        try:
            sess.start(system_prompt=args.system_prompt)
        except Exception as e:
            print(f"error: failed to start {backend_name}: {e}", file=sys.stderr)
            return EXIT_ERROR
        cleanup_after = is_oneshot
    elif args.system_prompt:
        print(
            "warning: --system-prompt ignored (session already running)",
            file=sys.stderr,
        )

    try:
        if _should_stream(args):
            try:
                for chunk in sess.stream(message, timeout=args.timeout):
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                print()
                return EXIT_OK
            except MenuPending as e:
                print()
                _emit_menu(e.menu, as_json=args.json, backend=backend_name, session=session_name)
                return EXIT_MENU
            except TimeoutError as e:
                print(f"\nerror: {e}", file=sys.stderr)
                return EXIT_TIMEOUT
        else:
            try:
                result = sess.send(message, timeout=args.timeout)
            except TimeoutError as e:
                print(f"error: {e}", file=sys.stderr)
                return EXIT_TIMEOUT
            if result.state == State.MENU:
                _emit_menu(result.menu, as_json=args.json, backend=backend_name, session=session_name)
                return EXIT_MENU
            _emit_response(
                result.text,
                as_json=args.json,
                backend=backend_name,
                session=args.session,
            )
            return EXIT_OK
    finally:
        if cleanup_after:
            sess.stop()


# --- session subcommands ---

def cmd_session_list(args: argparse.Namespace) -> int:
    rows = list_sessions()
    if not rows:
        if args.json:
            print(json.dumps([]))
        else:
            print("(no sessions)")
        return EXIT_OK
    if args.json:
        print(json.dumps([{"backend": b, "name": n} for b, n in rows]))
    else:
        for backend, name in rows:
            print(f"{backend}\t{name}")
    return EXIT_OK


def _find_session(name: str) -> tuple[str, str] | None:
    for backend, sess_name in list_sessions():
        if sess_name == name:
            return backend, sess_name
    return None


def cmd_session_kill(args: argparse.Namespace) -> int:
    found = _find_session(args.name)
    if not found:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    backend_name, name = found
    sess = Session(name=name, backend=get_backend(backend_name))
    if sess.stop():
        print(f"stopped: {backend_name}/{name}")
        return EXIT_OK
    print(f"error: could not stop {backend_name}/{name}", file=sys.stderr)
    return EXIT_ERROR


def cmd_session_show(args: argparse.Namespace) -> int:
    found = _find_session(args.name)
    if not found:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    backend_name, name = found
    sess = Session(name=name, backend=get_backend(backend_name))
    sys.stdout.write(sess.capture())
    return EXIT_OK


def cmd_session_menu(args: argparse.Namespace) -> int:
    found = _find_session(args.name)
    if not found:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    backend_name, name = found
    sess = Session(name=name, backend=get_backend(backend_name))
    pane = sess.capture()
    if not sess.backend.is_menu(pane):
        if args.json:
            print(json.dumps({"status": "no_menu"}))
        else:
            print("(no menu pending)")
        return EXIT_OK
    menu = sess.backend.extract_menu(pane)
    _emit_menu(menu, as_json=args.json, backend=backend_name, session=name)
    return EXIT_MENU


def cmd_session_choose(args: argparse.Namespace) -> int:
    found = _find_session(args.name)
    if not found:
        print(f"error: no session named {args.name!r}", file=sys.stderr)
        return EXIT_ERROR
    backend_name, name = found
    sess = Session(name=name, backend=get_backend(backend_name))
    try:
        choice: int | str = int(args.choice)
    except ValueError:
        choice = args.choice
    try:
        result = sess.respond_to_menu(choice)
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    if result.state == State.MENU:
        _emit_menu(result.menu, as_json=args.json, backend=backend_name, session=name)
        return EXIT_MENU
    _emit_response(result.text, as_json=args.json, backend=backend_name, session=name)
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
            "By default a one-shot session is used. Pass --session NAME to "
            "reuse a persistent session across calls."
        ),
    )
    p.add_argument("message", nargs="?", default=None, help="message (or read from stdin)")
    p.add_argument("--session", "-s", default=None, help="reuse a named persistent session")
    p.add_argument("--timeout", "-t", type=float, default=120.0, help="seconds to wait (default 120)")
    p.add_argument("--system-prompt", default=None, help="appended to backend system prompt (start only)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of plain text")
    p.add_argument("--no-stream", action="store_true", help="never stream, even to a tty")
    p.set_defaults(func=lambda a, bn=backend_name: cmd_backend(a, bn))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kage",
        description="Scriptable bridge for interactive AI CLIs (Claude Code, Codex, Gemini).",
    )
    parser.add_argument("--version", "-V", action="version", version=f"kage {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    for backend_name in list_backends():
        _add_backend_subparser(sub, backend_name)

    sp = sub.add_parser("session", help="manage long-lived sessions")
    sess_sub = sp.add_subparsers(dest="session_cmd", required=True, metavar="ACTION")

    p = sess_sub.add_parser("list", help="list running sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_list)

    p = sess_sub.add_parser("kill", help="kill a session")
    p.add_argument("name")
    p.set_defaults(func=cmd_session_kill)

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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
