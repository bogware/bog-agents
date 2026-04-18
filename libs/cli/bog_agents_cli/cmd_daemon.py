"""bog-agents daemon management commands."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # noqa: S404
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_DAEMON_DIR = Path.home() / ".bog-agents" / "daemon"
_PID_FILE = _DAEMON_DIR / "daemon.pid"
_TOKEN_FILE = _DAEMON_DIR / "token"
_DEFAULT_PORT = 7391


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _daemon_url(port: int = _DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def _read_token() -> str | None:
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    return None


def _read_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text().strip())
    except ValueError:
        return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _api_get(path: str, *, port: int = _DEFAULT_PORT) -> Any:  # noqa: ANN401
    token = _read_token()
    url = f"{_daemon_url(port)}{path}"
    req = urllib.request.Request(url, headers={"X-Daemon-Token": token or ""})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Public command handlers
# ---------------------------------------------------------------------------


def cmd_daemon_start(port: int = _DEFAULT_PORT, log_level: str = "INFO") -> None:
    """Start the daemon as a background process.

    Args:
        port: Port for the daemon REST API.
        log_level: Logging level for the daemon.
    """
    pid = _read_pid()
    if pid is not None and _is_running(pid):
        print(f"Daemon is already running (PID {pid}).")  # noqa: T201
        return

    exe = shutil.which("bog-agents-daemon")
    if exe is None:
        print(  # noqa: T201
            "bog-agents-daemon not found on PATH.\n"
            "Install it with: pip install bog-agents-daemon"
        )
        sys.exit(1)

    proc = subprocess.Popen(  # noqa: S603
        [exe, "--port", str(port), "--log-level", log_level],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Brief wait for daemon to bind and write PID file
    for _ in range(20):
        time.sleep(0.25)
        if _PID_FILE.exists():
            break

    print(f"Daemon started (PID {proc.pid}) on port {port}.")  # noqa: T201


def cmd_daemon_stop() -> None:
    """Stop a running daemon by sending SIGTERM to its PID."""
    pid = _read_pid()
    if pid is None:
        print("Daemon is not running (no PID file).")  # noqa: T201
        return
    if not _is_running(pid):
        print(f"Daemon PID {pid} is not running. Cleaning up stale PID file.")  # noqa: T201
        _PID_FILE.unlink(missing_ok=True)
        return
    os.kill(pid, signal.SIGTERM)
    # Wait up to 5 s for graceful shutdown
    for _ in range(20):
        time.sleep(0.25)
        if not _is_running(pid):
            break
    if _is_running(pid):
        print(f"Daemon (PID {pid}) did not stop in time; sending SIGKILL.")  # noqa: T201
        os.kill(pid, signal.SIGKILL)
    else:
        print(f"Daemon (PID {pid}) stopped.")  # noqa: T201


def cmd_daemon_status(port: int = _DEFAULT_PORT) -> None:
    """Print daemon running status and job count.

    Args:
        port: Port the daemon is listening on.
    """
    pid = _read_pid()
    running = pid is not None and _is_running(pid)

    if not running:
        print("Daemon: STOPPED" + (f" (stale PID {pid})" if pid else ""))  # noqa: T201
        return

    print(f"Daemon: RUNNING (PID {pid}, port {port})")  # noqa: T201

    try:
        health = _api_get("/health", port=port)
        print(  # noqa: T201
            f"  Version   : {health.get('version', '?')}\n"
            f"  Jobs      : {health.get('job_count', '?')}\n"
            f"  API       : {_daemon_url(port)}"
        )
    except (urllib.error.URLError, OSError):
        print(f"  API       : {_daemon_url(port)} (unreachable)")  # noqa: T201


def cmd_daemon_jobs(port: int = _DEFAULT_PORT) -> None:
    """List all configured ambient jobs.

    Args:
        port: Port the daemon is listening on.
    """
    try:
        jobs: list[dict[str, Any]] = _api_get("/jobs", port=port)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Cannot reach daemon at {_daemon_url(port)}: {exc}")  # noqa: T201
        print("Start it with: bog-agents daemon start")  # noqa: T201
        sys.exit(1)

    if not jobs:
        print("No jobs configured. Create one via the daemon REST API.")  # noqa: T201
        return

    print(f"{'ID':<36}  {'Name':<24}  {'Status':<12}  {'Runs':>5}  Enabled")  # noqa: T201
    print("-" * 90)  # noqa: T201
    for j in jobs:
        print(  # noqa: T201
            f"{j.get('job_id', '?'):<36}  "
            f"{j.get('name', '?')[:24]:<24}  "
            f"{j.get('last_status', '?'):<12}  "
            f"{j.get('run_count', 0):>5}  "
            f"{'yes' if j.get('enabled') else 'no'}"
        )


def cmd_daemon_install(*, platform: str | None = None) -> None:
    """Install the daemon as a systemd (Linux) or launchd (macOS) service.

    Args:
        platform: Override platform detection ('systemd' or 'launchd').
    """
    try:
        from bog_agents_daemon.install import install_launchd, install_systemd
    except ImportError:
        print(  # noqa: T201
            "bog-agents-daemon is not installed.\n"
            "Install it with: pip install bog-agents-daemon"
        )
        sys.exit(1)

    exe = shutil.which("bog-agents-daemon")
    if exe is None:
        print("bog-agents-daemon not found on PATH. Install it first.")  # noqa: T201
        sys.exit(1)

    resolved_platform = platform or ("launchd" if sys.platform == "darwin" else "systemd")

    if resolved_platform == "launchd":
        instructions = install_launchd(exe)
        print(instructions)  # noqa: T201
    else:
        instructions = install_systemd(exe)
        print(instructions)  # noqa: T201


def cmd_daemon_install_git_hook(repo: str, port: int = _DEFAULT_PORT) -> None:
    """Install a git post-receive hook that fires daemon git-push triggers.

    Args:
        repo: Path to the git repository root.
        port: Daemon port for the hook to POST to.
    """
    try:
        from bog_agents_daemon.install import install_git_hook
    except ImportError:
        print("bog-agents-daemon is not installed.")  # noqa: T201
        sys.exit(1)

    token = _read_token() or ""
    try:
        instructions = install_git_hook(repo, daemon_url=_daemon_url(port), token=token)
        print(instructions)  # noqa: T201
    except FileNotFoundError as exc:
        print(f"Error: {exc}")  # noqa: T201
        sys.exit(1)


def setup_daemon_parser(subparsers: Any) -> None:  # noqa: ANN401
    """Register the 'daemon' subcommand and its sub-subcommands.

    Args:
        subparsers: The argparse subparsers object from the main parser.
    """
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Manage the bog-agents ambient agent daemon",
        add_help=True,
        description=(
            "Control the ambient agent daemon — start/stop the service, "
            "check status, list jobs, and install as a system service."
        ),
    )
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")

    # start
    start_p = daemon_sub.add_parser("start", help="Start the daemon in the background")
    start_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port (default 7391)")
    start_p.add_argument("--log-level", default="INFO", help="Log level (default INFO)")

    # stop
    daemon_sub.add_parser("stop", help="Stop the running daemon")

    # status
    status_p = daemon_sub.add_parser("status", help="Show daemon status and job count")
    status_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs
    jobs_p = daemon_sub.add_parser("jobs", help="List configured ambient jobs")
    jobs_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # install
    install_p = daemon_sub.add_parser(
        "install",
        help="Install daemon as a systemd (Linux) or launchd (macOS) service",
    )
    install_p.add_argument(
        "--platform",
        choices=["systemd", "launchd"],
        default=None,
        help="Force a specific init system (auto-detected by default)",
    )

    # install-git-hook
    hook_p = daemon_sub.add_parser(
        "install-git-hook",
        help="Install a git post-receive hook that triggers daemon jobs on push",
    )
    hook_p.add_argument("--repo", required=True, help="Path to the git repository")
    hook_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="Daemon API port")


def execute_daemon_command(args: Any) -> None:  # noqa: ANN401
    """Dispatch a parsed 'daemon' subcommand to the appropriate handler.

    Args:
        args: Parsed argparse namespace with daemon_command attribute.

    Raises:
        SystemExit: When no subcommand is given (exits 0 after printing help).
    """
    cmd = getattr(args, "daemon_command", None)

    if cmd == "start":
        cmd_daemon_start(port=args.port, log_level=args.log_level)
    elif cmd == "stop":
        cmd_daemon_stop()
    elif cmd == "status":
        cmd_daemon_status(port=args.port)
    elif cmd == "jobs":
        cmd_daemon_jobs(port=args.port)
    elif cmd == "install":
        cmd_daemon_install(platform=getattr(args, "platform", None))
    elif cmd == "install-git-hook":
        cmd_daemon_install_git_hook(repo=args.repo, port=args.port)
    else:
        # No sub-subcommand: show daemon help

        print(  # noqa: T201
            "bog-agents daemon — ambient agent daemon management\n\n"
            "Commands:\n"
            "  start              Start the daemon in the background\n"
            "  stop               Stop the running daemon\n"
            "  status             Show daemon health and job count\n"
            "  jobs               List configured ambient jobs\n"
            "  install            Register as a system service (systemd/launchd)\n"
            "  install-git-hook   Install git post-receive hook for git-push triggers\n\n"
            "Run 'bog-agents daemon <command> --help' for details."
        )
        raise SystemExit(0)
