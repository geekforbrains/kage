"""kage command-line interface."""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import select
import signal
import shutil
import subprocess
import sys
import time

from . import __version__
from . import tmux as tmuxlib
from .backends import get_backend, list_backends
from .session import (
    BackendFailure,
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
EXIT_INTERACTION_REQUIRED = 10
EXIT_MENU = EXIT_INTERACTION_REQUIRED
EXIT_BUSY = 11
EXIT_TIMEOUT = 124
_TMUX_ENV_PREFIXES = ("ENSO_ORIGIN_",)
DEFAULT_SESSION_BACKEND = "claude"


# --- input/output helpers ---

def _read_available(fd: int, timeout: float = 0.2) -> str:
    """Read whatever is already on `fd` without blocking on EOF.

    Used when a message argument is present and stdin is only optional context:
    a plain ``sys.stdin.read()`` would block forever if the caller holds stdin
    open without sending EOF (a parent agent, an inherited pipe, a fifo). We
    drain what's ready and stop, rather than waiting for a close that may never
    come.
    """
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except (OSError, ValueError):
        return ""  # not a real/selectable fd; skip optional stdin
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not data:
                break  # EOF
            chunks.append(data)
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        except (OSError, ValueError):
            pass
    return b"".join(chunks).decode("utf-8", "replace")


def _resolve_message(args: argparse.Namespace) -> str:
    parts: list[str] = []
    message = getattr(args, "message", None)
    if message:
        parts.append(message)
    if not sys.stdin.isatty():
        if message:
            # stdin is optional context here — never block waiting for EOF.
            try:
                fd = sys.stdin.fileno()
            except (OSError, ValueError, AttributeError):
                fd = None
            stdin_data = _read_available(fd) if fd is not None else ""
        else:
            # No message argument: stdin IS the message, so block for it.
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
    """Resolve output mode."""
    if getattr(args, "stream", False):
        return "stream"
    if getattr(args, "json", False):
        return "json"
    return "text"


def _emit_json(payload: dict, *, flush: bool = False) -> None:
    print(json.dumps(payload), flush=flush)


def _tmux_env() -> dict[str, str]:
    """Environment values that must reach the backend inside tmux."""
    return {
        key: value
        for key, value in os.environ.items()
        if any(key.startswith(prefix) for prefix in _TMUX_ENV_PREFIXES)
    }


def _emit_response(text: str, *, mode: str, backend: str, session: str | None, session_id: str | None) -> None:
    if mode in ("json", "stream"):
        _emit_json({
            "status": "done",
            "backend": backend,
            "session": session,
            "session_id": session_id,
            "response": text,
        }, flush=mode == "stream")
    else:
        print(text)


def _emit_progress(event, *, mode: str, backend: str, session: str | None,
                   session_id: str | None) -> None:
    if mode != "stream":
        return
    _emit_json({
        "status": "progress",
        "backend": backend,
        "session": session,
        "session_id": session_id,
        "ts": event.ts,
        "event": event.event,
        "tool": event.tool,
        "summary": event.summary,
    }, flush=True)


def _emit_interaction_required(menu, *, mode: str, backend: str, session: str | None,
                               session_id: str | None) -> None:
    question = menu.question if menu else "interactive input requested"
    message = (
        "Claude Code paused on an interactive TUI prompt. kage runs in "
        f"autonomous mode; inspect or reset the session instead. Prompt: {question}"
    )
    if mode in ("json", "stream"):
        _emit_json({
            "status": "error",
            "backend": backend,
            "session": session,
            "session_id": session_id,
            "reason": "interaction_required",
            "message": message,
        }, flush=mode == "stream")
        return
    print(f"error: {message}", file=sys.stderr)


def _emit_error(
    *,
    mode: str,
    backend: str,
    session: str | None,
    session_id: str | None,
    reason: str,
    message: str,
) -> None:
    if mode in ("json", "stream"):
        _emit_json({
            "status": "error",
            "backend": backend,
            "session": session,
            "session_id": session_id,
            "reason": reason,
            "message": message,
        }, flush=mode == "stream")
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
            model=args.model,
            effort=args.effort,
            system_prompt=getattr(args, "system_prompt", None),
        ), False
    return Session.ephemeral(backend), True


def _install_signal_cleanup(sess: Session):
    """Stop the tmux session if the supervising kage process is interrupted."""
    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {}

    def handler(signum, frame):
        try:
            sess.stop()
        finally:
            raise SystemExit(128 + signum)

    for sig in watched:
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        except (OSError, ValueError):
            continue

    def restore() -> None:
        for sig, prior in previous.items():
            try:
                signal.signal(sig, prior)
            except (OSError, ValueError):
                pass

    return restore


# --- backend dispatch (the primary command form) ---

def cmd_backend(args: argparse.Namespace, backend_name: str) -> int:
    message = _resolve_message(args)
    mode = _output_mode(args)
    sess, cleanup_after = _build_session(args, backend_name)
    restart = bool(getattr(args, "restart", False))
    start_kwargs = {
        "system_prompt": getattr(args, "system_prompt", None),
        "model": args.model,
        "effort": args.effort,
        "env": _tmux_env(),
    }
    if mode == "stream":
        start_kwargs["progress"] = True

    restore_signal_cleanup = None
    if getattr(args, "stop_on_signal", False):
        restore_signal_cleanup = _install_signal_cleanup(sess)

    try:
        if not sess.exists():
            try:
                sess.start(**start_kwargs)
            except Exception as e:
                # Another kage process may have created the same tmux session
                # between our exists() check and new-session. If it exists now,
                # proceed; Session.send() will serialize access with the lock.
                if not sess.exists():
                    _emit_error(
                        mode=mode,
                        backend=backend_name,
                        session=args.session,
                        session_id=sess.session_id,
                        reason="start_failed",
                        message=f"failed to start {backend_name}: {e}",
                    )
                    return EXIT_ERROR
        else:
            restarted = False
            if restart:
                sess.stop()
                try:
                    sess.start(**start_kwargs)
                    restarted = True
                except Exception as e:
                    _emit_error(
                        mode=mode,
                        backend=backend_name,
                        session=args.session,
                        session_id=sess.session_id,
                        reason="start_failed",
                        message=f"failed to restart {backend_name}: {e}",
                    )
                    return EXIT_ERROR
            elif mode == "stream" and not sess.has_progress_hooks():
                pane = sess.capture()
                if sess.backend.is_busy(pane) or sess.backend.is_menu(pane):
                    print(
                        "warning: --stream requested but the running session was "
                        "started without progress hooks; restart the idle session "
                        "to enable progress events",
                        file=sys.stderr,
                    )
                else:
                    sess.stop()
                    try:
                        sess.start(**start_kwargs)
                        restarted = True
                    except Exception as e:
                        _emit_error(
                            mode=mode,
                            backend=backend_name,
                            session=args.session,
                            session_id=sess.session_id,
                            reason="start_failed",
                            message=f"failed to restart {backend_name} with progress hooks: {e}",
                        )
                        return EXIT_ERROR
            for flag in ("system_prompt", "model", "effort"):
                if not restarted and getattr(args, flag, None):
                    print(
                        f"warning: --{flag.replace('_', '-')} ignored (session already running)",
                        file=sys.stderr,
                    )
        rc = _run_send(sess, message, args, backend_name, mode)
        return rc
    finally:
        if restore_signal_cleanup is not None:
            restore_signal_cleanup()
        if cleanup_after:
            sess.stop()


def _run_send(sess: Session, message: str, args, backend_name: str, mode: str) -> int:
    def on_event(event) -> None:
        _emit_progress(
            event,
            mode=mode,
            backend=backend_name,
            session=args.session,
            session_id=sess.session_id,
        )

    try:
        result = sess.send(
            message,
            timeout=args.timeout,
            wait_if_busy=not getattr(args, "no_wait", False),
            on_event=on_event if mode == "stream" else None,
        )
    except SessionBusy as e:
        _emit_error(
            mode=mode,
            backend=backend_name,
            session=args.session,
            session_id=sess.session_id,
            reason="busy",
            message=str(e),
        )
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
        _emit_error(
            mode=mode,
            backend=backend_name,
            session=args.session,
            session_id=sess.session_id,
            reason="timeout",
            message=str(e),
        )
        return EXIT_TIMEOUT
    if result.state == State.MENU:
        _emit_interaction_required(result.menu, mode=mode, backend=backend_name,
                                   session=args.session, session_id=sess.session_id)
        return EXIT_INTERACTION_REQUIRED
    _emit_response(result.text, mode=mode, backend=backend_name,
                   session=args.session, session_id=sess.session_id)
    return EXIT_OK


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


def _find_session(name: str) -> tuple[str, str]:
    """Return (backend, resolved_name) for a session.

    Falls back to running tmux session if no record (anonymous sessions).
    """
    rec = state_mod.get(name)
    if rec:
        return rec.backend, name
    for backend, slug in list_sessions():
        if slug == name or slug_matches(name, slug):
            return backend, slug
    raise KeyError(name)


def _session_for_name(name: str) -> tuple[str, str, Session]:
    backend_name, resolved_name = _find_session(name)
    if state_mod.get(resolved_name):
        sess = Session.named(resolved_name, get_backend(backend_name))
    else:
        sess = Session(backend=get_backend(backend_name), slug=resolved_name)
    return backend_name, resolved_name, sess


def _load_session(name: str) -> tuple[str, str, Session] | None:
    try:
        return _session_for_name(name)
    except KeyError:
        print(f"error: no session named {name!r}", file=sys.stderr)
        return None


def _session_for_target(args: argparse.Namespace) -> tuple[str, str, Session] | None:
    """Resolve either a named session target or a caller-owned session id."""
    name = getattr(args, "name", None)
    session_id = getattr(args, "session_id", None)
    if name and session_id:
        print("error: session name and --session-id are mutually exclusive", file=sys.stderr)
        return None
    if session_id:
        backend_name = getattr(args, "backend", None) or DEFAULT_SESSION_BACKEND
        try:
            backend = get_backend(backend_name)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return None
        return backend_name, f"id:{session_id[:8]}", Session.by_id(session_id, backend)
    if name:
        return _load_session(name)
    print("error: pass a session name or --session-id", file=sys.stderr)
    return None


def cmd_session_kill(args: argparse.Namespace) -> int:
    resolved = _session_for_target(args)
    if resolved is None:
        return EXIT_ERROR
    backend_name, name, sess = resolved
    if sess.stop():
        print(f"stopped: {backend_name}/{name}")
        return EXIT_OK
    print(f"(no running tmux pane for {backend_name}/{name}; record preserved)")
    return EXIT_OK


def cmd_session_rm(args: argparse.Namespace) -> int:
    """Stop tmux AND forget the persisted record."""
    resolved = _load_session(args.name)
    if resolved is None:
        return EXIT_ERROR
    backend_name, name, sess = resolved
    rec = state_mod.get(name)
    if rec:
        sess.stop()
        sess.forget()
        print(f"removed: {backend_name}/{name}")
    else:
        sess.stop()
        print(f"stopped anonymous: {backend_name}/{name}")
    return EXIT_OK


def cmd_session_show(args: argparse.Namespace) -> int:
    resolved = _session_for_target(args)
    if resolved is None:
        return EXIT_ERROR
    _backend_name, name, sess = resolved
    if not sess.exists():
        print(f"(session {name!r} has no running tmux pane)", file=sys.stderr)
        return EXIT_ERROR
    sys.stdout.write(sess.capture())
    return EXIT_OK


def cmd_session_menu(args: argparse.Namespace) -> int:
    resolved = _load_session(args.name)
    if resolved is None:
        return EXIT_ERROR
    backend_name, name, sess = resolved
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
    _emit_interaction_required(menu, mode="json" if args.json else "text",
                               backend=backend_name, session=name,
                               session_id=sess.session_id)
    return EXIT_INTERACTION_REQUIRED


def cmd_session_choose(args: argparse.Namespace) -> int:
    resolved = _load_session(args.name)
    if resolved is None:
        return EXIT_ERROR
    backend_name, name, sess = resolved
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
        _emit_interaction_required(result.menu, mode=mode, backend=backend_name,
                                   session=name, session_id=sess.session_id)
        return EXIT_INTERACTION_REQUIRED
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


def cmd_session_prune(args: argparse.Namespace) -> int:
    """Stop sessions idle past a TTL and reap orphaned hook/event artifacts."""
    from . import session as _sess

    try:
        ttl = _sess.parse_duration(args.older_than)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE

    now_dt = _dt.datetime.now(_dt.timezone.utc)
    now_epoch = time.time()
    dry = getattr(args, "dry_run", False)

    stale = _sess.plan_idle_records(list(known_records()), now_dt, ttl)
    stopped: list[str] = []
    for rec in stale:
        sess = Session.named(rec.name, get_backend(rec.backend))
        if not dry:
            sess.stop()
        stopped.append(rec.name)

    running = _sess.list_sessions()
    running_tmux = {f"{_sess.SESSION_PREFIX}{b}_{slug}" for b, slug in running}
    orphans = _sess.find_orphan_artifacts(running_tmux, now=now_epoch, ttl=ttl)
    removed: list[str] = []
    for p in orphans:
        if not dry:
            try:
                p.unlink()
            except OSError:
                pass
        removed.append(str(p))

    if getattr(args, "json", False):
        print(json.dumps({"stopped": stopped, "removed_artifacts": removed, "dry_run": dry}))
        return EXIT_OK
    stop_verb = "would stop" if dry else "stopped"
    rm_verb = "would remove" if dry else "removed"
    for n in stopped:
        print(f"{stop_verb}: {n}")
    for p in removed:
        print(f"{rm_verb} artifact: {p}")
    if not stopped and not removed:
        print("nothing to prune")
    return EXIT_OK


def cmd_session_clear(args: argparse.Namespace) -> int:
    resolved = _session_for_target(args)
    if resolved is None:
        return EXIT_ERROR
    _backend_name, name, sess = resolved
    old_session_id = sess.session_id
    sess.reset()
    if args.json:
        payload = {
            "status": "cleared",
            "session": getattr(args, "name", None),
            "session_id": sess.session_id,
        }
        if getattr(args, "session_id", None):
            payload["cleared_session_id"] = old_session_id
        print(json.dumps(payload))
    else:
        print(f"cleared: {name} (new session_id={sess.session_id})")
    return EXIT_OK


# --- meta commands ---

def cmd_backends(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(list_backends()))
    else:
        for b in list_backends():
            print(b)
    return EXIT_OK


def cmd_hook(args: argparse.Namespace) -> int:
    """Append a hook-event record. Invoked by Claude Code, reads payload stdin."""
    from pathlib import Path
    from . import hooks as hooks_mod
    return hooks_mod.handle_hook(args.event, Path(args.events_file))


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
            f"Send a message to {backend_name} through its TUI and print the response. "
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
                   help="seconds to wait for a response (default 120). Raise it "
                        "for long agentic turns; on expiry kage exits 124.")
    p.add_argument("--model", default=None,
                   help="model alias to pass through (e.g. opus, sonnet)")
    p.add_argument("--effort", default=None,
                   help="effort level to pass through (low, medium, high, xhigh, max)")
    p.add_argument("--json", action="store_true",
                   help="emit a JSON envelope instead of plain response text")
    p.add_argument("--stream", action="store_true",
                   help="emit newline-delimited JSON progress events and final response")
    p.add_argument("--restart", action="store_true",
                   help="restart an existing tmux pane before sending")
    p.add_argument("--stop-on-signal", action="store_true",
                   help="stop the tmux pane if kage receives SIGINT or SIGTERM")
    p.set_defaults(func=lambda a, bn=backend_name: cmd_backend(a, bn))


def _add_session_target_args(p: argparse.ArgumentParser, *, help_text: str) -> None:
    p.add_argument("name", nargs="?", help=help_text)
    p.add_argument("--session-id", default=None,
                   help="target a caller-managed backend session UUID")
    p.add_argument("--backend", default=DEFAULT_SESSION_BACKEND,
                   choices=list_backends(),
                   help=f"backend for --session-id (default {DEFAULT_SESSION_BACKEND})")


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
    _add_session_target_args(p, help_text="session name or running slug")
    p.set_defaults(func=cmd_session_kill)

    p = sess_sub.add_parser("rm", help="stop and forget a named session")
    p.add_argument("name")
    p.set_defaults(func=cmd_session_rm)

    p = sess_sub.add_parser("show", help="dump raw pane (debug)")
    _add_session_target_args(p, help_text="session name or running slug")
    p.set_defaults(func=cmd_session_show)

    p = sess_sub.add_parser("compact", help="run /compact on a running named session")
    p.add_argument("name")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_compact)

    p = sess_sub.add_parser(
        "clear",
        help="clear context: kill tmux and rotate to a fresh underlying UUID",
    )
    _add_session_target_args(p, help_text="session name or running slug")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_clear)

    p = sess_sub.add_parser(
        "prune",
        help="stop idle sessions and reap orphaned hook/event artifacts",
    )
    p.add_argument("--older-than", default="24h",
                   help="idle threshold: e.g. 30m, 2h, 7d, or seconds (default 24h)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be pruned without stopping or deleting")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_session_prune)

    p = sub.add_parser("backends", help="list available backends")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backends)

    p = sub.add_parser("doctor", help="check environment and dependencies")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    # Internal: invoked by Claude Code hooks (see kage.hooks). Not for direct use.
    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("event")
    p.add_argument("--events-file", required=True)
    p.set_defaults(func=cmd_hook)

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
