"""bog-agents daemon management commands."""

from __future__ import annotations

import datetime
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


def _api_post(path: str, payload: dict[str, Any], *, port: int = _DEFAULT_PORT) -> Any:  # noqa: ANN401
    token = _read_token()
    url = f"{_daemon_url(port)}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-Daemon-Token": token or "", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _api_delete(path: str, *, port: int = _DEFAULT_PORT) -> int:
    token = _read_token()
    url = f"{_daemon_url(port)}{path}"
    req = urllib.request.Request(
        url,
        headers={"X-Daemon-Token": token or ""},
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def _unreachable(port: int) -> None:
    print(f"Cannot reach daemon at {_daemon_url(port)}.")  # noqa: T201
    print("Start it with: bog-agents daemon start")  # noqa: T201
    sys.exit(1)


def _trigger_summary(t: dict[str, Any]) -> str:
    """Return a short human-readable description of a trigger dict."""
    tt = t.get("type", "")
    if tt == "cron":
        return t.get("cron", "")
    if tt == "interval":
        return f"every {t.get('interval_seconds')}s"
    if tt == "file_change":
        return f"{t.get('watch_dir')} {t.get('watch_patterns', [])}"
    if tt == "webhook":
        return t.get("webhook_path", "")
    if tt == "git_push":
        return f"branch: {t.get('git_branch_pattern', '*')}"
    return ""


# ---------------------------------------------------------------------------
# Public command handlers — service lifecycle
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


# ---------------------------------------------------------------------------
# Public command handlers — job management
# ---------------------------------------------------------------------------


def cmd_jobs_list(port: int = _DEFAULT_PORT) -> None:
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
        print("No jobs configured. Use 'bog-agents daemon jobs create' to add one.")  # noqa: T201
        return

    print(f"{'ID':<14}  {'Name':<24}  {'Status':<12}  {'Runs':>5}  Enabled")  # noqa: T201
    print("-" * 70)  # noqa: T201
    for j in jobs:
        print(  # noqa: T201
            f"{j.get('job_id', '?'):<14}  "
            f"{str(j.get('name', '?'))[:24]:<24}  "
            f"{j.get('last_status', '?'):<12}  "
            f"{j.get('run_count', 0):>5}  "
            f"{'yes' if j.get('enabled') else 'no'}"
        )


def cmd_jobs_create(args: Any) -> None:  # noqa: ANN401
    """Create a new ambient job.

    Args:
        args: Parsed argparse namespace with job creation fields.
    """
    port: int = args.port

    triggers: list[dict[str, Any]] = []
    if args.cron:
        triggers.append({"type": "cron", "cron": args.cron})
    if args.interval:
        triggers.append({"type": "interval", "interval_seconds": int(args.interval)})
    if args.watch_dir:
        patterns: list[str] = args.watch_pattern or ["*"]
        triggers.append({
            "type": "file_change",
            "watch_dir": args.watch_dir,
            "watch_patterns": patterns,
            "debounce_seconds": float(args.debounce),
        })
    if args.webhook_path:
        triggers.append({"type": "webhook", "webhook_path": args.webhook_path})
    if args.git_branch:
        triggers.append({"type": "git_push", "git_branch_pattern": args.git_branch})

    output_target: str = args.output
    out: dict[str, Any] = {"target": output_target}
    if output_target == "file":
        out["file_path"] = args.output_file
        out["append"] = True
    elif output_target == "slack":
        out["slack_webhook_url"] = args.output_slack
    elif output_target == "webhook":
        out["webhook_url"] = args.output_webhook

    payload: dict[str, Any] = {
        "name": args.name,
        "prompt": args.prompt,
        "description": args.description,
        "model": args.model,
        "working_dir": args.working_dir,
        "pipeline_name": args.pipeline,
        "skill_name": args.skill,
        "triggers": triggers,
        "outputs": [out],
        "enabled": not args.disabled,
    }

    try:
        job = _api_post("/jobs", payload, port=port)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"Error {exc.code}: {body}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)

    print(f"Created job {job['job_id']}  {job['name']}")  # noqa: T201
    for t in job.get("triggers", []):
        print(f"  trigger : {t.get('type'):<12}  {_trigger_summary(t)}")  # noqa: T201
    print(f"  enabled : {'yes' if job.get('enabled') else 'no'}")  # noqa: T201


def cmd_jobs_show(job_id: str, port: int = _DEFAULT_PORT) -> None:
    """Show detailed information about a job.

    Args:
        job_id: The job identifier.
        port: Port the daemon is listening on.
    """
    try:
        job = _api_get(f"/jobs/{job_id}", port=port)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)

    print(f"ID          : {job['job_id']}")  # noqa: T201
    print(f"Name        : {job['name']}")  # noqa: T201
    if job.get("description"):
        print(f"Description : {job['description']}")  # noqa: T201
    print(f"Enabled     : {'yes' if job.get('enabled') else 'no'}")  # noqa: T201
    print(f"Status      : {job.get('last_status', '?')}")  # noqa: T201
    print(f"Runs        : {job.get('run_count', 0)}")  # noqa: T201
    if job.get("prompt"):
        print(f"Prompt      : {job['prompt']}")  # noqa: T201
    if job.get("model"):
        print(f"Model       : {job['model']}")  # noqa: T201
    if job.get("pipeline_name"):
        print(f"Pipeline    : {job['pipeline_name']}")  # noqa: T201
    if job.get("skill_name"):
        print(f"Skill       : {job['skill_name']}")  # noqa: T201
    if job.get("triggers"):
        print("Triggers    :")  # noqa: T201
        for t in job["triggers"]:
            print(f"  {t.get('type', '?'):<14}  {_trigger_summary(t)}")  # noqa: T201
    if job.get("outputs"):
        print("Outputs     :")  # noqa: T201
        for o in job["outputs"]:
            print(f"  {o.get('target', '?')}")  # noqa: T201


def cmd_jobs_delete(job_id: str, port: int = _DEFAULT_PORT) -> None:
    """Delete a job.

    Args:
        job_id: The job identifier.
        port: Port the daemon is listening on.
    """
    try:
        _api_delete(f"/jobs/{job_id}", port=port)
        print(f"Deleted job {job_id}.")  # noqa: T201
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)


def cmd_jobs_run(job_id: str, port: int = _DEFAULT_PORT) -> None:
    """Trigger an immediate manual run of a job and wait for the result.

    Args:
        job_id: The job identifier.
        port: Port the daemon is listening on.
    """
    try:
        run = _api_post(f"/jobs/{job_id}/run", {}, port=port)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)

    print(f"Run {run.get('run_id')}  status={run.get('status')}")  # noqa: T201
    if run.get("output"):
        print(run["output"])  # noqa: T201
    if run.get("error"):
        print(f"Error: {run['error']}")  # noqa: T201


def cmd_jobs_enable(job_id: str, port: int = _DEFAULT_PORT) -> None:
    """Enable a disabled job.

    Args:
        job_id: The job identifier.
        port: Port the daemon is listening on.
    """
    try:
        job = _api_post(f"/jobs/{job_id}/enable", {}, port=port)
        print(f"Job {job['job_id']} ({job['name']}) enabled.")  # noqa: T201
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)


def cmd_jobs_disable(job_id: str, port: int = _DEFAULT_PORT) -> None:
    """Disable a job without deleting it.

    Args:
        job_id: The job identifier.
        port: Port the daemon is listening on.
    """
    try:
        job = _api_post(f"/jobs/{job_id}/disable", {}, port=port)
        print(f"Job {job['job_id']} ({job['name']}) disabled.")  # noqa: T201
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)


def cmd_jobs_history(job_id: str | None = None, port: int = _DEFAULT_PORT) -> None:
    """Show run history for a specific job or all jobs.

    Args:
        job_id: The job identifier, or None to show all recent runs.
        port: Port the daemon is listening on.
    """
    try:
        if job_id:
            runs: list[dict[str, Any]] = _api_get(f"/jobs/{job_id}/runs", port=port)
        else:
            runs = _api_get("/runs", port=port)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)

    if not runs:
        print("No run history found.")  # noqa: T201
        return

    print(f"{'Run ID':<14}  {'Job':<20}  {'Status':<12}  {'Trigger':<12}  Started")  # noqa: T201
    print("-" * 80)  # noqa: T201
    for r in runs:
        started = r.get("started_at", 0)
        ts = datetime.datetime.fromtimestamp(started, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M") if started else "?"
        print(  # noqa: T201
            f"{r.get('run_id', '?'):<14}  "
            f"{str(r.get('job_name', '?'))[:20]:<20}  "
            f"{r.get('status', '?'):<12}  "
            f"{r.get('trigger_type', '?'):<12}  "
            f"{ts}"
        )


# ---------------------------------------------------------------------------
# Parser setup
# ---------------------------------------------------------------------------


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
            "check status, manage jobs, and install as a system service."
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

    # jobs — subcommand group
    jobs_parser = daemon_sub.add_parser(
        "jobs",
        help="Manage ambient jobs (create, list, show, delete, run, enable, disable, history)",
        description="Manage ambient agent jobs. Run 'bog-agents daemon jobs <command> --help' for details.",
    )
    jobs_parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command")

    # jobs list
    jobs_list_p = jobs_sub.add_parser("list", help="List all configured jobs")
    jobs_list_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs create
    create_p = jobs_sub.add_parser("create", help="Create a new ambient job")
    create_p.add_argument("--name", required=True, help="Job name")
    create_p.add_argument("--prompt", default="", help="Prompt to run (or use --pipeline / --skill)")
    create_p.add_argument("--description", default="", help="Optional human-readable description")
    create_p.add_argument("--model", default="", metavar="MODEL", help="Model override (e.g. anthropic:claude-sonnet-4-6)")
    create_p.add_argument("--working-dir", dest="working_dir", default="", metavar="DIR", help="Working directory for the agent")
    create_p.add_argument("--pipeline", default="", metavar="NAME", help="Run a saved pipeline instead of a raw prompt")
    create_p.add_argument("--skill", default="", metavar="NAME", help="Run a skill instead of a raw prompt")
    # trigger flags
    create_p.add_argument("--cron", default="", metavar="EXPR", help="Cron schedule, e.g. '0 9 * * 1-5'")
    create_p.add_argument("--interval", default=0, type=int, metavar="SECONDS", help="Run every N seconds")
    create_p.add_argument("--watch-dir", dest="watch_dir", default="", metavar="DIR", help="Directory to watch for file changes")
    create_p.add_argument(
        "--watch-pattern", dest="watch_pattern", action="append", metavar="GLOB",
        help="Glob pattern for file-change trigger (repeatable, default '*')",
    )
    create_p.add_argument("--debounce", default=5.0, type=float, metavar="SECONDS", help="File-change debounce delay (default 5s)")
    create_p.add_argument("--webhook-path", dest="webhook_path", default="", metavar="PATH", help="Webhook path suffix, e.g. /hooks/ci")
    create_p.add_argument("--git-branch", dest="git_branch", default="", metavar="PATTERN", help="Git branch glob for git-push trigger, e.g. main")
    # output flags
    create_p.add_argument(
        "--output", default="log",
        choices=["log", "stdout", "file", "slack", "webhook"],
        help="Output target (default: log)",
    )
    create_p.add_argument("--output-file", dest="output_file", default="", metavar="PATH", help="File path when --output=file")
    create_p.add_argument("--output-slack", dest="output_slack", default="", metavar="URL", help="Slack webhook URL when --output=slack")
    create_p.add_argument("--output-webhook", dest="output_webhook", default="", metavar="URL", help="Webhook URL when --output=webhook")
    create_p.add_argument("--disabled", action="store_true", help="Create the job in a disabled state")
    create_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs show <id>
    show_p = jobs_sub.add_parser("show", help="Show details for a job")
    show_p.add_argument("job_id", help="Job ID")
    show_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs delete <id>
    delete_p = jobs_sub.add_parser("delete", help="Delete a job")
    delete_p.add_argument("job_id", help="Job ID")
    delete_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs run <id>
    run_p = jobs_sub.add_parser("run", help="Trigger an immediate manual run of a job")
    run_p.add_argument("job_id", help="Job ID")
    run_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs enable <id>
    enable_p = jobs_sub.add_parser("enable", help="Enable a disabled job")
    enable_p.add_argument("job_id", help="Job ID")
    enable_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs disable <id>
    disable_p = jobs_sub.add_parser("disable", help="Disable a job without deleting it")
    disable_p.add_argument("job_id", help="Job ID")
    disable_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs history [id]
    history_p = jobs_sub.add_parser("history", help="Show run history for a job or all jobs")
    history_p.add_argument("job_id", nargs="?", default=None, help="Job ID (omit to show all recent runs)")
    history_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

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
        _execute_jobs_command(args)
    elif cmd == "install":
        cmd_daemon_install(platform=getattr(args, "platform", None))
    elif cmd == "install-git-hook":
        cmd_daemon_install_git_hook(repo=args.repo, port=args.port)
    else:
        print(  # noqa: T201
            "bog-agents daemon — ambient agent daemon management\n\n"
            "Commands:\n"
            "  start              Start the daemon in the background\n"
            "  stop               Stop the running daemon\n"
            "  status             Show daemon health and job count\n"
            "  jobs               Manage ambient jobs\n"
            "  install            Register as a system service (systemd/launchd)\n"
            "  install-git-hook   Install git post-receive hook for git-push triggers\n\n"
            "Run 'bog-agents daemon <command> --help' for details."
        )
        raise SystemExit(0)


def _execute_jobs_command(args: Any) -> None:  # noqa: ANN401
    """Dispatch a 'daemon jobs' subcommand to the appropriate handler.

    Args:
        args: Parsed argparse namespace with jobs_command attribute.

    Raises:
        SystemExit: When no subcommand is given (exits 0 after printing help).
    """
    jobs_cmd = getattr(args, "jobs_command", None)
    port: int = getattr(args, "port", _DEFAULT_PORT)

    if jobs_cmd is None or jobs_cmd == "list":
        cmd_jobs_list(port=port)
    elif jobs_cmd == "create":
        cmd_jobs_create(args)
    elif jobs_cmd == "show":
        cmd_jobs_show(args.job_id, port=port)
    elif jobs_cmd == "delete":
        cmd_jobs_delete(args.job_id, port=port)
    elif jobs_cmd == "run":
        cmd_jobs_run(args.job_id, port=port)
    elif jobs_cmd == "enable":
        cmd_jobs_enable(args.job_id, port=port)
    elif jobs_cmd == "disable":
        cmd_jobs_disable(args.job_id, port=port)
    elif jobs_cmd == "history":
        cmd_jobs_history(job_id=getattr(args, "job_id", None), port=port)
    else:
        print(  # noqa: T201
            "bog-agents daemon jobs — ambient job management\n\n"
            "Commands:\n"
            "  list               List all configured jobs\n"
            "  create             Create a new job\n"
            "  show <id>          Show job details\n"
            "  delete <id>        Delete a job\n"
            "  run <id>           Trigger an immediate manual run\n"
            "  enable <id>        Enable a disabled job\n"
            "  disable <id>       Disable a job without deleting it\n"
            "  history [id]       Show run history (omit id for all jobs)\n\n"
            "Run 'bog-agents daemon jobs <command> --help' for details."
        )
        raise SystemExit(0)
