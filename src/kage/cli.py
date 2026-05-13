"""kage command-line interface."""
from __future__ import annotations

import argparse
import sys

from .backends import get_backend, list_backends
from .session import (
    MenuPending,
    Session,
    State,
    list_sessions,
)


def _make_session(backend_name: str, name: str) -> Session:
    return Session(name=name, backend=get_backend(backend_name))


def cmd_backends(args: argparse.Namespace) -> int:
    for b in list_backends():
        print(b)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    sessions = list_sessions()
    if not sessions:
        print("(no sessions)")
        return 0
    for backend, name in sessions:
        print(f"{backend}\t{name}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    s.start(system_prompt=args.system_prompt)
    print(f"started: {args.backend}/{args.name}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    if s.stop():
        print(f"stopped: {args.backend}/{args.name}")
        return 0
    print(f"no session: {args.backend}/{args.name}", file=sys.stderr)
    return 1


def cmd_send(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    if not s.exists():
        s.start(system_prompt=args.system_prompt)
    result = s.send(args.message, timeout=args.timeout)
    if result.state == State.MENU:
        return _print_menu(result.menu)
    print(result.text)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """One-shot: fresh session, send, print, cleanup."""
    s = _make_session(args.backend, args.name)
    if s.exists():
        s.stop()
    s.start(system_prompt=args.system_prompt)
    try:
        result = s.send(args.message, timeout=args.timeout)
        if result.state == State.MENU:
            return _print_menu(result.menu)
        print(result.text)
        return 0
    finally:
        s.stop()


def cmd_stream(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    if not s.exists():
        s.start(system_prompt=args.system_prompt)
    try:
        for chunk in s.stream(args.message, timeout=args.timeout):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        print()
        return 0
    except MenuPending as e:
        print()
        return _print_menu(e.menu)


def cmd_menu(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    if not s.exists():
        print(f"no session: {args.backend}/{args.name}", file=sys.stderr)
        return 1
    pane = s.capture()
    if not s.backend.is_menu(pane):
        print("(no menu pending)")
        return 0
    menu = s.backend.extract_menu(pane)
    return _print_menu(menu)


def cmd_choose(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    if not s.exists():
        print(f"no session: {args.backend}/{args.name}", file=sys.stderr)
        return 1
    try:
        choice: int | str = int(args.choice)
    except ValueError:
        choice = args.choice
    result = s.respond_to_menu(choice)
    if result.state == State.MENU:
        return _print_menu(result.menu)
    print(result.text)
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    s = _make_session(args.backend, args.name)
    if not s.exists():
        print(f"no session: {args.backend}/{args.name}", file=sys.stderr)
        return 1
    sys.stdout.write(s.capture())
    return 0


def _print_menu(menu) -> int:
    if not menu:
        print("(menu pending but unparseable)", file=sys.stderr)
        return 2
    print(f"MENU: {menu.question}")
    for i, opt in enumerate(menu.options, start=1):
        print(f"  {i}. {opt}")
    print("respond with: kage choose <backend> <name> <number>")
    return 10  # distinct exit code for "needs input"


def _add_session_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("backend", choices=list_backends(), help="backend name")
    p.add_argument("name", nargs="?", default="default", help="session name (default: default)")


def _add_message_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("message", help="message to send")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for response")
    p.add_argument(
        "--system-prompt", default=None,
        help="appended to backend's system prompt (only takes effect on session start)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kage", description="Scriptable bridge for interactive AI CLIs.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backends", help="list available backends")
    p.set_defaults(func=cmd_backends)

    p = sub.add_parser("list", help="list running sessions")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("start", help="start a new session")
    _add_session_args(p)
    p.add_argument("--system-prompt", default=None)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop a session")
    _add_session_args(p)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("send", help="send a message to a session (auto-starts if absent)")
    _add_session_args(p)
    _add_message_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("run", help="one-shot: fresh session, send, cleanup")
    _add_session_args(p)
    _add_message_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("stream", help="send a message and stream the response")
    _add_session_args(p)
    _add_message_args(p)
    p.set_defaults(func=cmd_stream)

    p = sub.add_parser("menu", help="show the pending menu, if any")
    _add_session_args(p)
    p.set_defaults(func=cmd_menu)

    p = sub.add_parser("choose", help="answer a pending menu by number or text")
    _add_session_args(p)
    p.add_argument("choice", help="numeric choice or free-text response")
    p.set_defaults(func=cmd_choose)

    p = sub.add_parser("raw", help="dump the raw pane (debug)")
    _add_session_args(p)
    p.set_defaults(func=cmd_raw)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
