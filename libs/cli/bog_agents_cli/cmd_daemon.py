"""bog-agents daemon management commands."""

from __future__ import annotations

import contextlib
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

contextlib_suppress = contextlib.suppress

_DAEMON_DIR = Path.home() / ".bog-agents" / "daemon"
_PID_FILE = _DAEMON_DIR / "daemon.pid"
_TOKEN_FILE = _DAEMON_DIR / "token"
_START_LOCK_FILE = _DAEMON_DIR / "start.lock"
_DEFAULT_PORT = 7391


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _daemon_url(port: int = _DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def _read_token() -> str | None:
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    return None


def _read_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _is_running(pid: int) -> bool:
    """Return True if a process with the given PID is alive.

    Thin wrapper that delegates to the cross-platform helper in `_proc`.
    Kept as a module-private function for backwards compatibility with
    in-tree tests that monkeypatch this name.
    """
    from bog_agents_cli._proc import is_running

    return is_running(pid)


_MSYS_GIT_PREFIXES: tuple[str, ...] = (
    # Common Git-for-Windows install locations whose 'hooks/' directory
    # is the most likely false positive when MSYS rewrites '/hooks/...'.
    "C:/Program Files/Git",
    "C:\\Program Files\\Git",
    "C:/Program Files (x86)/Git",
    "C:\\Program Files (x86)\\Git",
)


def _strip_msys_path_mangle(value: str) -> str:
    """Recover an absolute-style arg that Git Bash converted into a Windows path.

    When a user passes ``--webhook-path /hooks/foo`` from MSYS / Git Bash on
    Windows, the MSYS path-conversion layer rewrites the arg to something
    like ``C:/Program Files/Git/hooks/foo`` *before* argparse sees it.
    Detect that exact mangle and restore the intended ``/hooks/foo``.

    Only triggers on Windows + when the value starts with a known Git
    install prefix and the next segment is the literal word ``hooks``.
    Anything else is returned unchanged so we never alter legitimate
    user-provided paths.

    Args:
        value: Raw arg string from argparse.

    Returns:
        The recovered path on detected mangle, otherwise the input verbatim.
    """
    if sys.platform != "win32" or not value:
        return value
    for prefix in _MSYS_GIT_PREFIXES:
        if value.startswith(prefix):
            tail = value[len(prefix) :].replace("\\", "/")
            if tail.startswith("/hooks/") or tail == "/hooks":
                return tail
    return value


def _find_daemon_executable() -> str | None:
    """Locate the bog-agents-daemon binary.

    Tries PATH first, then falls back to the directory containing the current
    Python interpreter so that `uv run` and venv-only installs (where the venv
    Scripts/bin dir is not on the host PATH) still work.

    Returns:
        Absolute path to the daemon executable, or None if not found.
    """
    found = shutil.which("bog-agents-daemon")
    if found is not None:
        return found

    interp_dir = Path(sys.executable).resolve().parent
    suffix = ".exe" if sys.platform == "win32" else ""
    for candidate_dir in (
        interp_dir,
        interp_dir.parent / "Scripts",
        interp_dir.parent / "bin",
    ):
        candidate = candidate_dir / f"bog-agents-daemon{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


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


def _api_patch(path: str, payload: dict[str, Any], *, port: int = _DEFAULT_PORT) -> Any:  # noqa: ANN401
    """PATCH ``path`` with the given JSON body and return the parsed response."""
    token = _read_token()
    url = f"{_daemon_url(port)}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-Daemon-Token": token or "", "Content-Type": "application/json"},
        method="PATCH",
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
    if tt == "github":
        return "GitHub events (assigned / labeled / comment / CI failure)"
    return ""


# ---------------------------------------------------------------------------
# Public command handlers — service lifecycle
# ---------------------------------------------------------------------------


def _acquire_start_lock() -> int | None:
    """Atomically create the daemon start lock-file.

    Returns the file descriptor on success, or ``None`` if another process
    already holds the lock (in which case the caller must not start a new
    daemon). Uses ``O_CREAT | O_EXCL`` for cross-platform atomicity. The
    lock-file is also stamped with the locker's PID so a stale lock left
    behind by a crashed `bog-agents daemon start` can be diagnosed.

    Raises:
        OSError: If writing the PID into the lock-file fails after we
            successfully created it; the lock-file is removed before
            re-raising so a retry can succeed.
    """
    _DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(_START_LOCK_FILE), flags, 0o600)
    except FileExistsError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        os.close(fd)
        with contextlib_suppress(OSError):
            _START_LOCK_FILE.unlink()
        raise
    return fd


def _release_start_lock(fd: int | None) -> None:
    if fd is not None:
        with contextlib_suppress(OSError):
            os.close(fd)
    with contextlib_suppress(OSError):
        _START_LOCK_FILE.unlink()


def cmd_daemon_start(port: int = _DEFAULT_PORT, log_level: str = "INFO") -> None:
    """Start the daemon as a background process.

    Idempotent: if a daemon is already running, prints its connection info
    and exits 0 instead of treating the duplicate-start as an error. Two
    concurrent ``bog-agents daemon start`` invocations are serialized via
    an exclusive lock-file to prevent both passing the alive-check and
    spawning racing daemons that fight over the same port.

    Args:
        port: Port for the daemon REST API.
        log_level: Logging level for the daemon.
    """
    pid = _read_pid()
    if pid is not None and _is_running(pid):
        print(f"Daemon is already running (PID {pid}) on {_daemon_url(port)}.")  # noqa: T201
        return

    lock_fd = _acquire_start_lock()
    if lock_fd is None:
        # Another start is in progress. Re-check pid once it likely finishes.
        for _ in range(20):
            time.sleep(0.25)
            pid = _read_pid()
            if pid is not None and _is_running(pid):
                print(  # noqa: T201
                    f"Daemon started by concurrent invocation (PID {pid}) on {_daemon_url(port)}."
                )
                return
        print(  # noqa: T201
            "Another `bog-agents daemon start` is in progress but did not finish in 5s.\n"
            f"Remove the stale lock with: rm '{_START_LOCK_FILE}' if no daemon is starting."
        )
        sys.exit(1)

    try:
        # Re-check inside the lock — the previous holder may have just started a daemon.
        pid = _read_pid()
        if pid is not None and _is_running(pid):
            print(f"Daemon is already running (PID {pid}) on {_daemon_url(port)}.")  # noqa: T201
            return

        exe = _find_daemon_executable()
        if exe is None:
            print(  # noqa: T201
                "bog-agents-daemon not found on PATH or in the CLI's environment.\n"
                "Install it with: pip install bog-agents-daemon"
            )
            sys.exit(1)

        # Pass env explicitly: on Windows the .exe shim + start_new_session
        # combination can drop ANTHROPIC_API_KEY (and other provider keys) from
        # the child's environment. Forward the CLI's full env so daemon-driven
        # jobs can reach LLM providers without the user having to set keys
        # again at the daemon level.
        proc = subprocess.Popen(  # noqa: S603
            [exe, "--port", str(port), "--log-level", log_level],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )

        # Brief wait for daemon to bind and write PID file
        for _ in range(20):
            time.sleep(0.25)
            if _PID_FILE.exists():
                break

        print(f"Daemon started (PID {proc.pid}) on port {port}.")  # noqa: T201
    finally:
        _release_start_lock(lock_fd)


def cmd_daemon_stop() -> None:
    """Stop a running daemon, preferring HTTP /shutdown over signal-based kill.

    Uses the daemon's REST `/shutdown` endpoint first (cross-platform and
    doesn't race with signal delivery), then falls back to `os.kill`. On
    Windows, `signal.SIGKILL` doesn't exist and `os.kill(SIGTERM)` already
    maps to TerminateProcess for a normal exit, so we don't need a separate
    SIGKILL path. All `os.kill` calls are wrapped against OSError so a
    stale PID file or a process that exits between the alive-check and
    the signal doesn't crash the CLI.
    """
    pid = _read_pid()
    if pid is None:
        print("Daemon is not running (no PID file).")  # noqa: T201
        return
    if not _is_running(pid):
        print(f"Daemon PID {pid} is not running. Cleaning up stale PID file.")  # noqa: T201
        _PID_FILE.unlink(missing_ok=True)
        return

    # 1) Try graceful HTTP shutdown first.
    token = _read_token() or ""
    try:
        url = f"{_daemon_url(_DEFAULT_PORT)}/shutdown"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={"X-Daemon-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, OSError):
        # Daemon may already be exiting, on a different port, or unauthorised.
        # Fall through to signal-based stop.
        pass

    # 2) Wait up to 5s for graceful exit (HTTP shutdown or otherwise).
    for _ in range(20):
        time.sleep(0.25)
        if not _is_running(pid):
            break

    # 3) Last resort: SIGTERM (Windows maps to TerminateProcess; POSIX to graceful term).
    if _is_running(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        for _ in range(8):
            time.sleep(0.25)
            if not _is_running(pid):
                break

    # 4) Stronger kill on POSIX only — SIGKILL doesn't exist on Windows.
    if _is_running(pid) and hasattr(signal, "SIGKILL"):
        print(f"Daemon (PID {pid}) did not stop in time; sending SIGKILL.")  # noqa: T201
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if _is_running(pid):
        print(f"Daemon (PID {pid}) is still running; manual cleanup may be needed.")  # noqa: T201
    else:
        _PID_FILE.unlink(missing_ok=True)
        print(f"Daemon (PID {pid}) stopped.")  # noqa: T201


def cmd_daemon_drain(
    port: int = _DEFAULT_PORT, *, timeout: float = 600.0, stop: bool = False
) -> int:
    """Ask the daemon to stop taking runs, wait for in-flight ones, optionally stop it (ROADMAP #56).

    Returns:
        0 drained, 1 unreachable, 2 timed out (the daemon stays drained; runs finish on their own).
    """
    import time as _time

    try:
        first = _api_post("/drain", {}, port=port)
    except Exception as exc:
        print(f"Could not reach the daemon: {exc}")  # noqa: T201
        return 1
    running = int(first.get("running", 0) or 0)
    print(f"Draining: {running} run(s) in flight; no new runs will start.")  # noqa: T201
    deadline = _time.monotonic() + timeout
    while running and _time.monotonic() < deadline:
        _time.sleep(2.0)
        try:
            running = int(_api_get("/health", port=port).get("running", 0) or 0)
        except Exception:
            break
    if running:
        print(  # noqa: T201
            f"Timed out after {timeout:.0f}s with {running} run(s) still in flight; the daemon stays drained."
        )
        return 2
    print("Drained: no runs in flight.")  # noqa: T201
    if stop:
        cmd_daemon_stop()
    return 0


def cmd_daemon_upgrade(port: int = _DEFAULT_PORT, *, timeout: float = 600.0) -> int:
    """Drain, stop, upgrade `bog-agents-daemon` with the tool that installed it, start again (ROADMAP #56)."""
    import shutil
    import subprocess  # noqa: S404
    import sys as _sys

    code = cmd_daemon_drain(port, timeout=timeout, stop=True)
    if code:
        return code
    uv = shutil.which("uv")
    cmd = (
        [uv, "tool", "upgrade", "bog-agents-daemon"]
        if uv
        else [_sys.executable, "-m", "pip", "install", "--upgrade", "bog-agents-daemon"]
    )
    print("Upgrading: " + " ".join(cmd))  # noqa: T201
    result = subprocess.run(cmd, check=False)  # noqa: S603
    if result.returncode:
        print(  # noqa: T201
            "Upgrade failed; the daemon is stopped — start it again with `bog-agents daemon start`."
        )
        return int(result.returncode)
    cmd_daemon_start(port=port)
    return 0


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


def cmd_daemon_rotate_token(port: int = _DEFAULT_PORT) -> None:
    """Rotate the daemon API token.

    Calls ``POST /admin/rotate-token`` with the current token, persists the
    new token to ``~/.bog-agents/daemon/token`` (the daemon does this server
    side, but we re-read it here so the local CLI session uses the new
    value immediately), and prints a confirmation. If the daemon isn't
    running, exits with a non-zero status.
    """
    pid = _read_pid()
    if pid is None or not _is_running(pid):
        print("Daemon is not running. Start it before rotating the token.")  # noqa: T201
        sys.exit(1)
    try:
        result = _api_post("/admin/rotate-token", {}, port=port)
    except (urllib.error.URLError, OSError) as e:
        print(f"Failed to rotate token: {e}")  # noqa: T201
        sys.exit(1)
    new_token = result.get("token") if isinstance(result, dict) else None
    if not new_token:
        print("Daemon did not return a new token.")  # noqa: T201
        sys.exit(1)
    print("Daemon API token rotated successfully.")  # noqa: T201
    print(f"New token written to {_TOKEN_FILE}")  # noqa: T201


def cmd_daemon_install(*, platform: str | None = None) -> None:
    """Install the daemon as a systemd (Linux), launchd (macOS) or Task Scheduler (Windows) service.

    Args:
        platform: Override platform detection ('systemd', 'launchd' or 'windows').
    """
    try:
        from bog_agents_daemon.install import (
            install_launchd,
            install_systemd,
            install_windows_task,
        )
    except ImportError:
        print(  # noqa: T201
            "bog-agents-daemon is not installed.\n"
            "Install it with: pip install bog-agents-daemon"
        )
        sys.exit(1)

    exe = _find_daemon_executable()
    if exe is None:
        print("bog-agents-daemon not found on PATH or in the CLI's environment.")  # noqa: T201
        sys.exit(1)

    if platform:
        resolved_platform = platform
    elif sys.platform == "darwin":
        resolved_platform = "launchd"
    elif sys.platform == "win32":
        # v6 DMN-3: used to fall through to systemd and write a useless unit file.
        resolved_platform = "windows"
    else:
        resolved_platform = "systemd"

    if resolved_platform == "launchd":
        instructions = install_launchd(exe)
        print(instructions)  # noqa: T201
    elif resolved_platform == "windows":
        instructions = install_windows_task(exe)
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


def _triggers_from_args(args: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Assemble the trigger dicts for `jobs create` from parsed CLI flags.

    Pure so the flag → trigger mapping is unit-testable without a daemon.

    Args:
        args: The parsed argparse namespace for `daemon jobs create`.

    Returns:
        Trigger dicts in the daemon API's JSON shape.
    """
    triggers: list[dict[str, Any]] = []
    if args.cron:
        triggers.append({"type": "cron", "cron": args.cron})
    if args.interval:
        triggers.append({"type": "interval", "interval_seconds": int(args.interval)})
    if args.watch_dir:
        patterns: list[str] = args.watch_pattern or ["*"]
        triggers.append(
            {
                "type": "file_change",
                "watch_dir": args.watch_dir,
                "watch_patterns": patterns,
                "debounce_seconds": float(args.debounce),
            }
        )
    if args.webhook_path:
        # MSYS / Git Bash on Windows rewrites a leading-slash arg into an
        # absolute path under the Git install (e.g. /hooks/foo becomes
        # 'C:/Program Files/Git/hooks/foo'). Detect that mangle and
        # recover the intended path. See cross-platform-notes.md.
        webhook_path = _strip_msys_path_mangle(args.webhook_path)
        triggers.append({"type": "webhook", "webhook_path": webhook_path})
    if args.git_branch:
        triggers.append({"type": "git_push", "git_branch_pattern": args.git_branch})
    if getattr(args, "github", False) or getattr(args, "github_number", 0):
        trigger: dict[str, Any] = {"type": "github"}
        if getattr(args, "github_number", 0):
            trigger["github_number"] = int(
                args.github_number
            )  # ROADMAP #55: PR / issue scoped
        triggers.append(trigger)
    return triggers


def cmd_jobs_create(args: Any) -> None:  # noqa: ANN401
    """Create a new ambient job.

    Args:
        args: Parsed argparse namespace with job creation fields.
    """
    port: int = args.port

    triggers = _triggers_from_args(args)

    output_target: str = args.output
    out: dict[str, Any] = {"target": output_target}
    if output_target == "file":
        out["file_path"] = args.output_file
        out["append"] = True
    elif output_target == "slack":
        out["slack_webhook_url"] = args.output_slack
    elif output_target == "webhook":
        out["webhook_url"] = args.output_webhook
    elif output_target == "email":
        # SMTP password is intentionally read from the arg directly so users
        # can either pass it inline or stuff it into a wrapper script that
        # reads from env. The daemon does not log it.
        to_addrs = [
            a.strip()
            for a in (getattr(args, "output_email_to", "") or "").split(",")
            if a.strip()
        ]
        out["to_addrs"] = to_addrs
        out["from_addr"] = getattr(args, "output_email_from", "") or ""
        out["smtp_host"] = getattr(args, "output_email_smtp_host", "") or ""
        out["smtp_port"] = getattr(args, "output_email_smtp_port", 587)
        out["smtp_username"] = getattr(args, "output_email_smtp_user", "") or ""
        out["smtp_password"] = getattr(args, "output_email_smtp_password", "") or ""
    elif output_target == "github_comment":
        out["github_repo"] = getattr(args, "output_github_repo", "") or ""
        raw_issue = str(getattr(args, "output_github_issue", "") or "").strip()
        # Digits become an int (the historical type); anything else is a
        # placeholder such as {pr_number} that the daemon renders at dispatch.
        out["github_issue_or_pr"] = (
            int(raw_issue) if raw_issue.isdigit() else (raw_issue or 0)
        )

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
    # ROADMAP #51: per-run budget (pauses at the cap) and per-job daily ceiling.
    if getattr(args, "budget_usd", None):
        payload["budget_usd"] = args.budget_usd
    # ROADMAP #55: attempt cap + the interactive thread the job continues.
    if getattr(args, "max_runs", 0):
        payload["max_runs"] = int(args.max_runs)
    if getattr(args, "thread", ""):
        payload["thread_id"] = args.thread
    if getattr(args, "daily_ceiling_usd", None):
        payload["daily_ceiling_usd"] = args.daily_ceiling_usd

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


def cmd_jobs_edit(args: Any, *, port: int = _DEFAULT_PORT) -> None:  # noqa: ANN401
    """Patch fields on an existing job via the daemon REST API.

    Pulls the user-supplied ``--prompt``, ``--model``, ``--enable``,
    ``--disable``, etc. from the argparse namespace and forwards a
    minimal payload to ``PATCH /jobs/{id}``. Fields the user did not
    specify are left untouched on the daemon side.
    """
    payload: dict[str, Any] = {}
    field_map = {
        "name": "name",
        "description": "description",
        "prompt": "prompt",
        "pipeline_name": "pipeline_name",
        "skill_name": "skill_name",
        "model": "model",
        "working_dir": "working_dir",
        "budget_usd": "budget_usd",
        "daily_ceiling_usd": "daily_ceiling_usd",
    }
    for attr, key in field_map.items():
        value = getattr(args, attr, None)
        if value is not None:
            payload[key] = value

    enabled = getattr(args, "enabled", None)
    if enabled is not None:
        payload["enabled"] = enabled

    if not payload:
        print("Nothing to update — pass at least one of --prompt/--name/--enable/etc.")  # noqa: T201
        sys.exit(2)

    try:
        result = _api_patch(f"/jobs/{args.job_id}", payload, port=port)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{args.job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)
        return

    name = result.get("name", args.job_id) if isinstance(result, dict) else args.job_id
    fields = ", ".join(sorted(payload))
    print(f"Updated job '{name}' ({args.job_id}) — fields: {fields}")  # noqa: T201


def cmd_jobs_resume(run_id: str, budget_usd: float, port: int = _DEFAULT_PORT) -> None:
    """Resume a budget-paused run with a raised cap (ROADMAP #51).

    Args:
        run_id: The paused run's id.
        budget_usd: The new per-run cap.
        port: Port the daemon is listening on.
    """
    try:
        resumed = _api_post(
            f"/runs/{run_id}/resume", {"budget_usd": budget_usd}, port=port
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Run '{run_id}' is not paused (or the daemon restarted).")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)
    rid = resumed.get("run_id", run_id)
    print(f"Run {rid} resumed with budget ${budget_usd:.2f}; see `jobs history`.")  # noqa: T201


def cmd_jobs_run(job_id: str, port: int = _DEFAULT_PORT) -> None:
    """Trigger a manual run of a job and poll until it completes.

    The daemon's `/jobs/{id}/run` endpoint returns immediately with a
    `running` placeholder so it never times out under HTTP. We then poll
    the run-history endpoint until the run reaches a terminal state.

    Args:
        job_id: The job identifier.
        port: Port the daemon is listening on.
    """
    try:
        triggered = _api_post(f"/jobs/{job_id}/run", {}, port=port)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Job '{job_id}' not found.")  # noqa: T201
        else:
            print(f"Error {exc.code}: {exc.reason}")  # noqa: T201
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        _unreachable(port)

    run_id = triggered.get("run_id", "")
    print(f"Run {run_id}  status=running (polling for completion)")  # noqa: T201

    deadline = (
        time.monotonic() + 1800
    )  # 30 min — matches daemon's BOG_DAEMON_AGENT_TIMEOUT
    final_run: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            runs = _api_get(f"/jobs/{job_id}/runs", port=port)
        except (urllib.error.URLError, OSError):
            time.sleep(2)
            continue
        for r in runs:
            if r.get("run_id") == run_id and r.get("status") in (
                "completed",
                "failed",
                "paused",
                "skipped",
            ):
                final_run = r
                break
        if final_run is not None:
            break
        time.sleep(3)

    if final_run is None:
        print(f"Run {run_id} did not finish within 30 minutes.")  # noqa: T201
        sys.exit(1)

    print(f"Run {final_run['run_id']}  status={final_run['status']}")  # noqa: T201
    if final_run.get("output"):
        print(final_run["output"])  # noqa: T201
    if final_run.get("error"):
        print(f"Error: {final_run['error']}")  # noqa: T201


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
        ts = (
            datetime.datetime.fromtimestamp(started, tz=datetime.UTC).strftime(
                "%Y-%m-%d %H:%M"
            )
            if started
            else "?"
        )
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
    start_p.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help="API port (default 7391)"
    )
    start_p.add_argument("--log-level", default="INFO", help="Log level (default INFO)")

    # stop
    daemon_sub.add_parser("stop", help="Stop the running daemon")

    # ROADMAP #56: drain before stop / upgrade so in-flight runs finish.
    drain_p = daemon_sub.add_parser(
        "drain",
        help="Stop taking new runs, wait for in-flight ones (then optionally stop)",
    )
    drain_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")
    drain_p.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for in-flight runs (default 600)",
    )
    drain_p.add_argument(
        "--stop", action="store_true", help="Stop the daemon once drained"
    )
    upgrade_p = daemon_sub.add_parser(
        "upgrade", help="Drain, stop, upgrade the daemon package, start it again"
    )
    upgrade_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")
    upgrade_p.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for in-flight runs (default 600)",
    )

    # status
    status_p = daemon_sub.add_parser("status", help="Show daemon status and job count")
    status_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # rotate-token
    rotate_p = daemon_sub.add_parser(
        "rotate-token",
        help="Rotate the daemon API token (invalidates the current token immediately)",
    )
    rotate_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

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
    create_p.add_argument(
        "--prompt", default="", help="Prompt to run (or use --pipeline / --skill)"
    )
    create_p.add_argument(
        "--description", default="", help="Optional human-readable description"
    )
    create_p.add_argument(
        "--model",
        default="",
        metavar="MODEL",
        help="Model override (e.g. anthropic:claude-sonnet-4-6)",
    )
    create_p.add_argument(
        "--working-dir",
        dest="working_dir",
        default="",
        metavar="DIR",
        help="Working directory for the agent",
    )
    create_p.add_argument(
        "--budget-usd",
        dest="budget_usd",
        type=float,
        default=None,
        metavar="USD",
        help="Per-run cost cap; the run pauses at the cap until `jobs resume <run_id> --budget-usd N` (#51)",
    )
    create_p.add_argument(
        "--daily-ceiling-usd",
        dest="daily_ceiling_usd",
        type=float,
        default=None,
        metavar="USD",
        help="Per-job daily spend ceiling; runs are skipped once today's spend reaches it (#51)",
    )
    create_p.add_argument(
        "--pipeline",
        default="",
        metavar="NAME",
        help="Run a saved pipeline instead of a raw prompt",
    )
    create_p.add_argument(
        "--skill",
        default="",
        metavar="NAME",
        help="Run a skill instead of a raw prompt",
    )
    # trigger flags
    create_p.add_argument(
        "--cron", default="", metavar="EXPR", help="Cron schedule, e.g. '0 9 * * 1-5'"
    )
    create_p.add_argument(
        "--interval", default=0, type=int, metavar="SECONDS", help="Run every N seconds"
    )
    create_p.add_argument(
        "--watch-dir",
        dest="watch_dir",
        default="",
        metavar="DIR",
        help="Directory to watch for file changes",
    )
    create_p.add_argument(
        "--watch-pattern",
        dest="watch_pattern",
        action="append",
        metavar="GLOB",
        help="Glob pattern for file-change trigger (repeatable, default '*')",
    )
    create_p.add_argument(
        "--debounce",
        default=5.0,
        type=float,
        metavar="SECONDS",
        help="File-change debounce delay (default 5s)",
    )
    create_p.add_argument(
        "--webhook-path",
        dest="webhook_path",
        default="",
        metavar="PATH",
        help="Webhook path suffix, e.g. /hooks/ci",
    )
    create_p.add_argument(
        "--git-branch",
        dest="git_branch",
        default="",
        metavar="PATTERN",
        help="Git branch glob for git-push trigger, e.g. main",
    )
    create_p.add_argument(
        "--github",
        dest="github",
        action="store_true",
        help=(
            "Fire on GitHub events delivered to POST /webhooks/github: an issue assigned to the bot, "
            "an opt-in label, an @-mention comment, or a CI failure (v6 DMN-2). Requires "
            "BOG_DAEMON_GITHUB_WEBHOOK_SECRET on the daemon; see the quickstart."
        ),
    )
    # output flags
    create_p.add_argument(
        "--output",
        default="log",
        choices=[
            "log",
            "stdout",
            "file",
            "slack",
            "webhook",
            "email",
            "github_comment",
        ],
        help="Output target (default: log). 'email' / 'github_comment' use the per-target flags below.",
    )
    create_p.add_argument(
        "--output-file",
        dest="output_file",
        default="",
        metavar="PATH",
        help="File path when --output=file",
    )
    create_p.add_argument(
        "--output-slack",
        dest="output_slack",
        default="",
        metavar="URL",
        help="Slack webhook URL when --output=slack",
    )
    create_p.add_argument(
        "--output-webhook",
        dest="output_webhook",
        default="",
        metavar="URL",
        help="Webhook URL when --output=webhook",
    )
    # email output (smtp_*, to_addrs, from_addr, subject_template)
    create_p.add_argument(
        "--output-email-to",
        dest="output_email_to",
        default="",
        metavar="ADDR[,ADDR...]",
        help="Comma-separated recipient list when --output=email",
    )
    create_p.add_argument(
        "--output-email-from",
        dest="output_email_from",
        default="",
        metavar="ADDR",
        help="From address when --output=email",
    )
    create_p.add_argument(
        "--output-email-smtp-host",
        dest="output_email_smtp_host",
        default="",
        metavar="HOST",
        help="SMTP host when --output=email (defaults to localhost)",
    )
    create_p.add_argument(
        "--output-email-smtp-port",
        dest="output_email_smtp_port",
        type=int,
        default=587,
        metavar="PORT",
        help="SMTP port when --output=email (587=STARTTLS, 465=SSL, 25=plain; default 587)",
    )
    create_p.add_argument(
        "--output-email-smtp-user",
        dest="output_email_smtp_user",
        default="",
        metavar="USER",
        help="SMTP username when --output=email",
    )
    create_p.add_argument(
        "--output-email-smtp-password",
        dest="output_email_smtp_password",
        default="",
        metavar="PW",
        help="SMTP password when --output=email (env-passable; do not commit)",
    )
    # github_comment output
    create_p.add_argument(
        "--output-github-repo",
        dest="output_github_repo",
        default="",
        metavar="OWNER/REPO",
        help="Repo when --output=github_comment",
    )
    create_p.add_argument(
        "--output-github-issue",
        dest="output_github_issue",
        type=str,
        default="",
        metavar="N|{pr_number}",
        help="Issue or PR number when --output=github_comment, or a placeholder such as {pr_number} / {number} resolved from the trigger context at dispatch",
    )
    create_p.add_argument(
        "--disabled", action="store_true", help="Create the job in a disabled state"
    )
    create_p.add_argument(
        "--max-runs",
        dest="max_runs",
        type=int,
        default=0,
        help="Disable the job after this many runs (0 = unlimited; #55 attempt cap)",
    )
    create_p.add_argument(
        "--thread",
        default="",
        metavar="THREAD_ID",
        help="Continue this interactive thread on each run (reopens the CLI checkpointer; #55)",
    )
    create_p.add_argument(
        "--github-number",
        dest="github_number",
        type=int,
        default=0,
        help="Scope a github trigger to one PR / issue number (#55)",
    )
    create_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs show <id>
    show_p = jobs_sub.add_parser("show", help="Show details for a job")
    show_p.add_argument("job_id", help="Job ID")
    show_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs delete <id>
    delete_p = jobs_sub.add_parser("delete", help="Delete a job")
    delete_p.add_argument("job_id", help="Job ID")
    delete_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs edit <id>
    edit_p = jobs_sub.add_parser(
        "edit",
        help="Edit fields of an existing job in place (PATCH /jobs/{id})",
    )
    edit_p.add_argument("job_id", help="Job ID")
    edit_p.add_argument("--name", help="New job name")
    edit_p.add_argument("--description", help="New description")
    edit_p.add_argument("--prompt", help="New prompt body")
    edit_p.add_argument(
        "--pipeline-name", dest="pipeline_name", help="New pipeline name"
    )
    edit_p.add_argument("--skill-name", dest="skill_name", help="New skill name")
    edit_p.add_argument(
        "--model", help="New model spec (e.g. anthropic:claude-sonnet-4-6)"
    )
    edit_p.add_argument(
        "--working-dir", dest="working_dir", help="New working directory"
    )
    edit_p.add_argument(
        "--budget-usd", dest="budget_usd", type=float, help="New per-run cost cap (#51)"
    )
    edit_p.add_argument(
        "--daily-ceiling-usd",
        dest="daily_ceiling_usd",
        type=float,
        help="New per-job daily ceiling (#51)",
    )
    edit_p.add_argument(
        "--enable",
        dest="enabled",
        action="store_const",
        const=True,
        help="Enable the job (mutually exclusive with --disable)",
    )
    edit_p.add_argument(
        "--disable",
        dest="enabled",
        action="store_const",
        const=False,
        help="Disable the job",
    )
    edit_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs run <id>
    run_p = jobs_sub.add_parser("run", help="Trigger an immediate manual run of a job")
    run_p.add_argument("job_id", help="Job ID")
    run_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs resume <run_id> --budget-usd N  (ROADMAP #51)
    resume_p = jobs_sub.add_parser(
        "resume", help="Resume a budget-paused run with a raised cap"
    )
    resume_p.add_argument("run_id", help="Run ID (status=paused)")
    resume_p.add_argument(
        "--budget-usd",
        dest="budget_usd",
        type=float,
        required=True,
        metavar="USD",
        help="New per-run cap",
    )
    resume_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs enable <id>
    enable_p = jobs_sub.add_parser("enable", help="Enable a disabled job")
    enable_p.add_argument("job_id", help="Job ID")
    enable_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs disable <id>
    disable_p = jobs_sub.add_parser("disable", help="Disable a job without deleting it")
    disable_p.add_argument("job_id", help="Job ID")
    disable_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # jobs history [id]
    history_p = jobs_sub.add_parser(
        "history", help="Show run history for a job or all jobs"
    )
    history_p.add_argument(
        "job_id", nargs="?", default=None, help="Job ID (omit to show all recent runs)"
    )
    history_p.add_argument("--port", type=int, default=_DEFAULT_PORT, help="API port")

    # install
    install_p = daemon_sub.add_parser(
        "install",
        help="Install daemon as a systemd (Linux), launchd (macOS) or Task Scheduler (Windows) service",
    )
    install_p.add_argument(
        "--platform",
        choices=["systemd", "launchd", "windows"],
        default=None,
        help="Force a specific init system (auto-detected by default)",
    )

    # install-git-hook
    hook_p = daemon_sub.add_parser(
        "install-git-hook",
        help="Install a git post-receive hook that triggers daemon jobs on push",
    )
    hook_p.add_argument("--repo", required=True, help="Path to the git repository")
    hook_p.add_argument(
        "--port", type=int, default=_DEFAULT_PORT, help="Daemon API port"
    )


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
    elif cmd == "drain":
        raise SystemExit(
            cmd_daemon_drain(port=args.port, timeout=args.timeout, stop=args.stop)
        )
    elif cmd == "upgrade":
        raise SystemExit(cmd_daemon_upgrade(port=args.port, timeout=args.timeout))
    elif cmd == "status":
        cmd_daemon_status(port=args.port)
    elif cmd == "rotate-token":
        cmd_daemon_rotate_token(port=args.port)
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
            "  rotate-token       Rotate the daemon API token\n"
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
    elif jobs_cmd == "edit":
        cmd_jobs_edit(args, port=port)
    elif jobs_cmd == "run":
        cmd_jobs_run(args.job_id, port=port)
    elif jobs_cmd == "resume":
        cmd_jobs_resume(args.run_id, args.budget_usd, port=port)
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
            "  edit <id> [--prompt …] [--enable|--disable] …  Edit fields in place\n"
            "  run <id>           Trigger an immediate manual run\n"
            "  enable <id>        Enable a disabled job\n"
            "  disable <id>       Disable a job without deleting it\n"
            "  history [id]       Show run history (omit id for all jobs)\n\n"
            "Run 'bog-agents daemon jobs <command> --help' for details."
        )
        raise SystemExit(0)
