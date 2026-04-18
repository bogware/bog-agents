"""Client for the bog-agents-daemon REST API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DAEMON_URL = "http://127.0.0.1:7391"
_TOKEN_FILE = Path.home() / ".bog-agents" / "daemon" / "token"
_PID_FILE = Path.home() / ".bog-agents" / "daemon" / "daemon.pid"


def is_daemon_running() -> bool:
    """Check whether the daemon process is alive.

    Reads the PID from the PID file and sends signal 0 to verify the process
    exists without killing it.

    Returns:
        True if the daemon is running, False otherwise.
    """
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def get_daemon_token() -> str | None:
    """Read the daemon authentication token from disk.

    Returns:
        The token string, or None if the token file does not exist.
    """
    if not _TOKEN_FILE.exists():
        return None
    try:
        return _TOKEN_FILE.read_text().strip()
    except OSError:
        return None


def _make_request(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Perform a blocking HTTP request to the daemon API.

    This function is intended to be called via `asyncio.to_thread` from async
    contexts. Uses only the standard library to avoid adding aiohttp as a
    dependency to the CLI.

    Args:
        method: HTTP method (e.g. "GET", "POST", "DELETE").
        path: URL path relative to the daemon base URL (must start with "/").
        json_body: Optional dict to JSON-encode as the request body.

    Returns:
        Parsed JSON response as a dict or list wrapped in a dict, or None on error.
    """
    token = get_daemon_token()
    if token is None:
        logger.debug("No daemon token found, skipping request to %s", path)
        return None

    url = f"{_DAEMON_URL}{path}"
    data: bytes | None = None
    headers: dict[str, str] = {"X-Daemon-Token": token, "Accept": "application/json"}

    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                return {}
            parsed = json.loads(raw)
            # Normalize list responses into a wrapper dict for uniform typing
            if isinstance(parsed, list):
                return {"items": parsed}
            return parsed
    except urllib.error.HTTPError as exc:
        logger.debug("Daemon API %s %s → %d", method, path, exc.code)
        return None
    except urllib.error.URLError as exc:
        logger.debug("Daemon API connection error for %s: %s", path, exc)
        return None
    except Exception:
        logger.debug("Daemon API unexpected error for %s", path, exc_info=True)
        return None


async def daemon_request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Make an authenticated async request to the daemon REST API.

    Offloads the blocking urllib call to a thread so the event loop is not
    blocked.

    Args:
        method: HTTP method (e.g. "GET", "POST", "DELETE").
        path: URL path relative to the daemon base URL.
        json: Optional JSON body payload.

    Returns:
        Parsed response dict, or None on connection or auth failure.
    """
    return await asyncio.to_thread(_make_request, method, path, json_body=json)


async def list_daemon_jobs() -> list[dict[str, Any]]:
    """Fetch the list of all configured ambient jobs from the daemon.

    Returns:
        List of job dicts; empty list if the daemon is unreachable.
    """
    result = await daemon_request("GET", "/jobs")
    if result is None:
        return []
    # Response may be wrapped as {"items": [...]} or a direct list promoted to dict
    if "items" in result:
        items = result["items"]
        return items if isinstance(items, list) else []
    return []


async def create_daemon_job(job: dict[str, Any]) -> dict[str, Any]:
    """Create a new ambient job on the daemon.

    Args:
        job: A dict conforming to the CreateJobRequest schema.

    Returns:
        The created job dict from the daemon, or an empty dict on failure.
    """
    result = await daemon_request("POST", "/jobs", json=job)
    return result or {}


async def trigger_daemon_job(job_id: str) -> dict[str, Any]:
    """Trigger an immediate manual run of a job.

    Args:
        job_id: The identifier of the job to run.

    Returns:
        The initiated JobRun dict, or an empty dict on failure.
    """
    result = await daemon_request("POST", f"/jobs/{job_id}/run")
    return result or {}


async def get_daemon_status() -> dict[str, Any] | None:
    """Fetch the daemon health status.

    Returns:
        A dict with keys "status", "version", "job_count", or None if
        the daemon is unreachable.
    """
    return await daemon_request("GET", "/health")


def format_daemon_status(status: dict[str, Any] | None, jobs: list[dict[str, Any]]) -> str:
    """Render daemon status and job list as Rich markup text.

    Args:
        status: The health response dict from `get_daemon_status`, or None.
        jobs: List of job dicts from `list_daemon_jobs`.

    Returns:
        A Rich markup string suitable for display in the TUI.
    """
    lines: list[str] = []

    if status is not None:
        version = status.get("version", "?")
        job_count = status.get("job_count", len(jobs))
        lines.append(f"[bold green]Daemon running[/bold green]  version=[cyan]{version}[/cyan]  jobs=[cyan]{job_count}[/cyan]")
    else:
        lines.append("[bold yellow]Daemon status unavailable[/bold yellow]")

    lines.append("")

    if not jobs:
        lines.append("[dim]No ambient jobs configured.[/dim]")
        lines.append("")
        lines.append("Create a job via the API or by editing [bold]~/.bog-agents/daemon/jobs.json[/bold].")
        return "\n".join(lines)

    # Header row
    col_id = 12
    col_name = 24
    col_status = 12
    col_runs = 6
    col_trigger = 14

    header = (
        f"{'ID':<{col_id}}  {'Name':<{col_name}}  {'Status':<{col_status}}  "
        f"{'Runs':>{col_runs}}  {'Trigger':<{col_trigger}}  Enabled"
    )
    lines.append(f"[bold]{header}[/bold]")
    lines.append("[dim]" + "-" * len(header) + "[/dim]")

    for job in jobs:
        job_id = (job.get("job_id") or "")[:col_id]
        name = (job.get("name") or "")[:col_name]
        last_status = job.get("last_status") or "pending"
        run_count = job.get("run_count", 0)
        enabled = job.get("enabled", True)
        triggers = job.get("triggers") or []
        trigger_types = ", ".join(sorted({t.get("type", "?") for t in triggers})) or "—"
        trigger_types = trigger_types[:col_trigger]

        status_color = {
            "completed": "green",
            "running": "cyan",
            "failed": "red",
            "cancelled": "yellow",
            "skipped": "dim",
            "pending": "white",
        }.get(last_status, "white")

        enabled_str = "[green]yes[/green]" if enabled else "[red]no[/red]"
        status_str = f"[{status_color}]{last_status:<{col_status}}[/{status_color}]"

        line = (
            f"{job_id:<{col_id}}  {name:<{col_name}}  {status_str}  "
            f"{run_count:>{col_runs}}  {trigger_types:<{col_trigger}}  {enabled_str}"
        )
        lines.append(line)

    lines.append("")
    lines.append("[dim]Use /ambient run <id> to trigger a job manually.[/dim]")
    return "\n".join(lines)
