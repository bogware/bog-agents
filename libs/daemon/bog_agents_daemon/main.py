"""bog-agents-daemon entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

import uvicorn

from bog_agents_daemon.api import create_app
from bog_agents_daemon.runner import run_job
from bog_agents_daemon.scheduler import DaemonScheduler
from bog_agents_daemon.store import load_jobs

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 7391
_TOKEN_FILE = Path.home() / ".bog-agents" / "daemon" / "token"
_PID_FILE = Path.home() / ".bog-agents" / "daemon" / "daemon.pid"


def _generate_token() -> str:
    """Generate a cryptographically secure random token.

    Returns:
        A URL-safe 32-byte random token string.
    """
    import secrets

    return secrets.token_urlsafe(32)


def _ensure_token() -> str:
    """Read or create the daemon auth token.

    If the token file does not exist, generates a new token, writes it with
    mode 0o600, and returns it. If it already exists, returns the existing value.

    On Windows, `chmod(0o600)` only flips the read-only bit — it does NOT
    restrict access by user. Modern Windows user-profile directories
    (`C:\\Users\\<user>\\`) are protected by ACLs that already deny other
    standard users, so this is fine on a single-user machine. On
    multi-user / shared systems the token file inherits its parent's
    ACL — operators who need stricter isolation should set
    `BOG_DAEMON_DATA_DIR` to a directory with explicit ACLs.

    Returns:
        The daemon auth token string.
    """
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    token = _generate_token()
    _TOKEN_FILE.write_text(token, encoding="utf-8")
    _TOKEN_FILE.chmod(0o600)
    # Windows-specific belt-and-suspenders: try icacls to grant only the
    # current user read/write, blocking inherited ACEs. Best-effort —
    # icacls failures don't fail token creation.
    if sys.platform == "win32":
        import contextlib
        import subprocess

        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if username:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["icacls", str(_TOKEN_FILE), "/inheritance:r", "/grant", f"{username}:F"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
    return token


def _write_pid() -> None:
    """Write the current process PID to the PID file."""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _clear_pid() -> None:
    """Remove the PID file on daemon shutdown.

    Silently ignores all errors since this is cleanup-only.
    """
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        logger.debug("Could not remove PID file on shutdown", exc_info=True)


async def _run_daemon(port: int, token: str) -> None:
    """Start the uvicorn server and scheduler concurrently.

    Installs SIGTERM/SIGINT handlers so that the daemon drains in-flight tasks
    before exiting. The TaskGroup propagates cancellation to both tasks when
    either completes or raises. Also exposes `POST /shutdown` so clients can
    request graceful termination over HTTP — useful on Windows where signal
    delivery via the PID file is unreliable.

    Args:
        port: TCP port to listen on.
        token: Auth token for API authentication.
    """
    scheduler = DaemonScheduler(store_loader=load_jobs, runner=run_job)

    config = uvicorn.Config(
        None,  # placeholder — we replace `app` immediately below
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # The TaskGroup below holds two tasks (server + scheduler). We need a way
    # to signal both when the HTTP /shutdown endpoint fires. Stash the
    # scheduler task on a closure-local ref so the callback can cancel it.
    scheduler_task: asyncio.Task[None] | None = None

    def _request_shutdown() -> None:
        logger.info("Graceful shutdown requested via API")
        server.should_exit = True
        if scheduler_task is not None and not scheduler_task.done():
            scheduler_task.cancel()

    app = create_app(token=token, scheduler=scheduler, request_shutdown=_request_shutdown)
    config.app = app

    loop = asyncio.get_running_loop()

    def _shutdown_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down", signum)
        server.should_exit = True

    def _reload_signal(signum: int, _frame: object) -> None:
        # SIGHUP is the conventional "reload config" signal for unix daemons.
        # We re-read jobs from disk so cron edits via the API land without
        # a full restart. Anything else is a no-op for now.
        logger.info("Received SIGHUP — reloading job store")
        try:
            scheduler.reload_jobs()
        except Exception:  # pragma: no cover — defensive
            logger.exception("SIGHUP reload failed")

    _signal_targets: list[tuple[Any, Any]] = [
        (signal.SIGTERM, _shutdown_signal),
        (signal.SIGINT, _shutdown_signal),
    ]
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        _signal_targets.append((sighup, _reload_signal))

    for sig, handler in _signal_targets:
        try:
            loop.add_signal_handler(sig, handler, sig, None)
        except (NotImplementedError, OSError):
            # Windows / environments that don't support add_signal_handler
            signal.signal(sig, handler)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(server.serve())
            scheduler_task = tg.create_task(scheduler.run_forever())
    except asyncio.CancelledError:
        # Expected when the scheduler is cancelled by /shutdown.
        pass
    finally:
        # Drain BOTH the scheduler's bg tasks and webhook-triggered jobs the
        # API may have spawned. Without webhook-task draining, a `systemctl
        # stop` arriving mid-webhook can lose in-flight runs.
        in_flight: set[asyncio.Task[Any]] = set(scheduler._bg_tasks)
        webhook_tasks = getattr(getattr(app, "state", None), "webhook_tasks", None)
        if webhook_tasks:
            in_flight.update(t for t in webhook_tasks if not t.done())
        if in_flight:
            logger.info("Waiting for %d in-flight job(s) to complete…", len(in_flight))
            _done, pending = await asyncio.wait(in_flight, timeout=30)
            for task in pending:
                task.cancel()


def _read_pid() -> int | None:
    """Read the daemon PID from the PID file, if it exists and is valid."""
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _stop_via_http(port: int, token: str, *, timeout: float = 5.0) -> bool:
    """Try to gracefully stop the daemon by calling POST /shutdown.

    Returns:
        True if the shutdown request was accepted (HTTP 202), False otherwise.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url=f"http://127.0.0.1:{port}/shutdown",
        method="POST",
        headers={"X-Daemon-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError:
        return False
    except Exception:  # we want to know the daemon's unreachable, not crash
        return False


def _force_kill(pid: int) -> bool:
    """Force-kill a daemon PID using the platform's strongest signal.

    Returns:
        True if a kill attempt was made (whether or not the process exited).
    """
    if sys.platform == "win32":
        try:
            import subprocess

            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                check=False,
                capture_output=True,
                timeout=5,
            )
            return True
        except OSError:
            return False
    try:
        import os as _os

        _os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, OSError):
        return False


def _cmd_start(port: int, log_level: str) -> int:
    """Run the daemon in the foreground."""
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO))
    token = _ensure_token()
    _write_pid()

    try:
        logger.info("bog-agents-daemon starting on port %d", port)
        asyncio.run(_run_daemon(port, token))
    except OSError as exc:
        # Port already in use or permission denied
        if getattr(exc, "errno", None) in (98, 48, 13):  # EADDRINUSE / EACCES
            logger.error(
                "Failed to bind to port %d: %s. "
                "Is another instance running? Use --port to choose a different port "
                "or `bog-agents-daemon stop` to stop the existing one.",
                port,
                exc.strerror,
            )
            return 1
        raise
    finally:
        _clear_pid()
    return 0


def _cmd_stop(port: int, *, force: bool, wait_seconds: float) -> int:
    """Stop a running daemon via HTTP, falling back to a force-kill.

    Returns:
        0 on clean stop (or if no daemon was running).
        1 on failure to stop.
    """
    if not _TOKEN_FILE.exists():
        print("No token file at", _TOKEN_FILE, "— is the daemon installed?")
        return 1

    token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    pid = _read_pid()

    # 1. Graceful shutdown via HTTP
    if _stop_via_http(port, token):
        # Wait briefly for the process to exit
        if pid is not None:
            import time

            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if not _process_alive(pid):
                    print("Daemon stopped.")
                    return 0
                time.sleep(0.2)
        else:
            print("Shutdown requested.")
            return 0

    # 2. If --force or graceful failed, force-kill via PID
    if force and pid is not None:
        if _force_kill(pid):
            print(f"Force-killed daemon PID {pid}.")
            _clear_pid()
            return 0
        print(f"Could not force-kill PID {pid}.")
        return 1

    if pid is None:
        print("No daemon PID found.")
        return 0

    print(
        f"Daemon (PID {pid}) did not respond to graceful shutdown. Re-run with --force to taskkill it.",
    )
    return 1


def _process_alive(pid: int) -> bool:
    """Check if a PID is still running. Cross-platform."""
    if sys.platform == "win32":
        import subprocess

        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                check=False,
                capture_output=True,
                timeout=3,
                text=True,
            )
            return str(pid) in (result.stdout or "")
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID belongs to another user but the process IS alive.
        return True
    except OSError:
        return False
    return True


def _cmd_status() -> int:
    """Print daemon status and return 0 if running, 1 if not."""
    pid = _read_pid()
    if pid is None:
        print("Daemon: not running (no PID file).")
        return 1
    if _process_alive(pid):
        print(f"Daemon: running (PID {pid}).")
        return 0
    print(f"Daemon: stale PID file ({pid}); process is not running.")
    return 1


def main() -> None:
    """Entry point for the bog-agents-daemon CLI command.

    Subcommands:
        start (default): run the daemon in the foreground.
        stop:            request graceful shutdown via HTTP, optionally
                         falling back to a platform force-kill.
        status:          print whether a daemon is running.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Bog Agents Daemon")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("start", help="Run the daemon (default)")

    stop_p = sub.add_parser("stop", help="Stop a running daemon")
    stop_p.add_argument(
        "--force",
        action="store_true",
        help="Fall back to a platform force-kill if graceful shutdown fails",
    )
    stop_p.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="Seconds to wait for the process to exit after graceful shutdown",
    )

    sub.add_parser("status", help="Show whether the daemon is running")

    args = parser.parse_args()
    cmd = args.cmd or "start"

    if cmd == "stop":
        sys.exit(_cmd_stop(args.port, force=args.force, wait_seconds=args.wait))
    if cmd == "status":
        sys.exit(_cmd_status())

    sys.exit(_cmd_start(args.port, args.log_level))


if __name__ == "__main__":
    main()
