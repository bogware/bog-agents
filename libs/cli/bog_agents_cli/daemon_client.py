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


async def get_daemon_job_runs(job_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Fetch recent run history for a specific job.

    Args:
        job_id: The identifier of the job to fetch runs for.
        limit: Maximum number of runs to return (most recent first).

    Returns:
        List of run dicts; empty list if the daemon is unreachable or job not found.
    """
    result = await daemon_request("GET", f"/jobs/{job_id}/runs")
    if result is None:
        return []
    items: list[dict[str, Any]] = []
    if "items" in result:
        raw = result["items"]
        items = raw if isinstance(raw, list) else []
    return items[:limit]


async def add_daemon_job(job_def: dict[str, Any]) -> dict[str, Any] | None:
    """Create a new ambient job on the daemon.

    This is a typed alias that mirrors `create_daemon_job` with an explicit
    return type of `dict | None` for callers that need to distinguish failure
    from an empty response.

    Args:
        job_def: A dict conforming to the CreateJobRequest schema.

    Returns:
        The created job dict from the daemon, or None on failure.
    """
    return await daemon_request("POST", "/jobs", json=job_def)


async def delete_daemon_job(job_id: str) -> bool:
    """Delete an ambient job from the daemon.

    Args:
        job_id: The identifier of the job to delete.

    Returns:
        True if the job was deleted (daemon returned 204 or an empty body), False
        if the daemon was unreachable or returned an error.
    """
    token = get_daemon_token()
    if token is None:
        logger.debug("No daemon token found, cannot delete job %s", job_id)
        return False

    url = f"{_DAEMON_URL}/jobs/{job_id}"
    headers: dict[str, str] = {"X-Daemon-Token": token, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers, method="DELETE")

    def _delete_blocking() -> bool:
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.debug("Delete job %s: not found (404)", job_id)
            else:
                logger.debug("Delete job %s: HTTP %d", job_id, exc.code)
            return False
        except urllib.error.URLError as exc:
            logger.debug("Delete job %s: connection error: %s", job_id, exc)
            return False

    return await asyncio.to_thread(_delete_blocking)


async def get_daemon_logs(job_id: str, *, run_id: str | None = None) -> str:
    """Retrieve the output text from a job run.

    Fetches the run list for the job and returns the output from the most recent
    run, or from the specific run identified by `run_id`.

    Args:
        job_id: The identifier of the job.
        run_id: If provided, return output for this specific run rather than
            the most recent one.

    Returns:
        The output text string, or an empty string if no runs are found.
    """
    runs = await get_daemon_job_runs(job_id, limit=50)
    if not runs:
        return ""
    if run_id is not None:
        for run in runs:
            if run.get("run_id") == run_id:
                return run.get("output") or run.get("error") or ""
        return ""
    # Return the most recent run's output (runs are assumed newest-first from the API)
    most_recent = runs[0]
    return most_recent.get("output") or most_recent.get("error") or ""


def _format_ts(ts: float | None) -> str:
    """Format a Unix timestamp as a compact human-readable string.

    Args:
        ts: Unix timestamp, or None / 0 for "never".

    Returns:
        A formatted date-time string like "2026-04-18 09:00" or "never".
    """
    import datetime

    if not ts:
        return "never"
    try:
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "?"


def format_daemon_status(
    status: dict[str, Any] | None,
    jobs: list[dict[str, Any]],
    *,
    runs_per_job: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """Render daemon status and job list as Rich markup text.

    When `runs_per_job` is provided, each job row is followed by a compact
    history of its most recent runs (up to 3).

    Args:
        status: The health response dict from `get_daemon_status`, or None.
        jobs: List of job dicts from `list_daemon_jobs`.
        runs_per_job: Optional mapping of job_id to a list of recent run dicts.

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
    col_last_run = 16

    header = (
        f"{'ID':<{col_id}}  {'Name':<{col_name}}  {'Status':<{col_status}}  "
        f"{'Runs':>{col_runs}}  {'Trigger':<{col_trigger}}  {'Last Run':<{col_last_run}}  Enabled"
    )
    lines.append(f"[bold]{header}[/bold]")
    lines.append("[dim]" + "-" * len(header) + "[/dim]")

    status_color_map = {
        "completed": "green",
        "running": "cyan",
        "failed": "red",
        "cancelled": "yellow",
        "skipped": "dim",
        "pending": "white",
    }

    for job in jobs:
        job_id_raw = job.get("job_id") or ""
        job_id = job_id_raw[:col_id]
        name = (job.get("name") or "")[:col_name]
        last_status = job.get("last_status") or "pending"
        run_count = job.get("run_count", 0)
        enabled = job.get("enabled", True)
        triggers = job.get("triggers") or []
        trigger_types = ", ".join(sorted({t.get("type", "?") for t in triggers})) or "—"
        trigger_types = trigger_types[:col_trigger]
        last_run_ts = job.get("last_run_at") or 0.0
        last_run_str = _format_ts(last_run_ts)[:col_last_run]

        color = status_color_map.get(last_status, "white")
        enabled_str = "[green]yes[/green]" if enabled else "[red]no[/red]"
        status_str = f"[{color}]{last_status:<{col_status}}[/{color}]"

        line = (
            f"{job_id:<{col_id}}  {name:<{col_name}}  {status_str}  "
            f"{run_count:>{col_runs}}  {trigger_types:<{col_trigger}}  {last_run_str:<{col_last_run}}  {enabled_str}"
        )
        lines.append(line)

        # Show recent run history if provided
        if runs_per_job:
            recent_runs = (runs_per_job.get(job_id_raw) or [])[:3]
            for run in recent_runs:
                run_status = run.get("status") or "?"
                run_color = status_color_map.get(run_status, "white")
                run_id_short = (run.get("run_id") or "")[:8]
                started = _format_ts(run.get("started_at") or 0.0)
                trigger = run.get("trigger_type") or "?"
                lines.append(
                    f"  [dim]↳ {run_id_short}  [{run_color}]{run_status}[/{run_color}]  {started}  via {trigger}[/dim]"
                )

    lines.append("")
    lines.append("[dim]Use /ambient run <id> to trigger a job manually.[/dim]")
    return "\n".join(lines)
