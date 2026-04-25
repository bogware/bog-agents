"""bog-agents-daemon entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

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

    Returns:
        The daemon auth token string.
    """
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    token = _generate_token()
    _TOKEN_FILE.write_text(token)
    _TOKEN_FILE.chmod(0o600)
    return token


def _write_pid() -> None:
    """Write the current process PID to the PID file."""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


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
    either completes or raises.

    Args:
        port: TCP port to listen on.
        token: Auth token for API authentication.
    """
    scheduler = DaemonScheduler(store_loader=load_jobs, runner=run_job)
    app = create_app(token=token, scheduler=scheduler)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def _shutdown_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down", signum)
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_signal, sig, None)
        except (NotImplementedError, OSError):
            # Windows / environments that don't support add_signal_handler
            signal.signal(sig, _shutdown_signal)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(server.serve())
            tg.create_task(scheduler.run_forever())
    finally:
        # Wait up to 30 seconds for in-flight background tasks to finish
        if scheduler._bg_tasks:
            logger.info("Waiting for %d in-flight job(s) to complete…", len(scheduler._bg_tasks))
            _done, pending = await asyncio.wait(scheduler._bg_tasks, timeout=30)
            for task in pending:
                task.cancel()


def main() -> None:
    """Entry point for the bog-agents-daemon CLI command.

    Parses arguments, ensures the auth token exists, writes the PID file,
    and runs the daemon until interrupted.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Bog Agents Daemon")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    token = _ensure_token()
    _write_pid()

    try:
        logger.info("bog-agents-daemon starting on port %d", args.port)
        asyncio.run(_run_daemon(args.port, token))
    except OSError as exc:
        # Port already in use or permission denied
        if getattr(exc, "errno", None) in (98, 48, 13):  # EADDRINUSE / EACCES
            logger.error(
                "Failed to bind to port %d: %s. "
                "Is another instance running? Use --port to choose a different port.",
                args.port, exc.strerror,
            )
            sys.exit(1)
        raise
    finally:
        _clear_pid()


if __name__ == "__main__":
    main()
