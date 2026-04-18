"""Persistent job store — JSON files in ~/.bog-agents/daemon/."""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any

from bog_agents_daemon.models import (
    AmbientJob,
    JobRun,
    JobStatus,
    OutputConfig,
    OutputTarget,
    TriggerConfig,
    TriggerType,
)

logger = logging.getLogger(__name__)

_DAEMON_DIR = Path.home() / ".bog-agents" / "daemon"
_JOBS_FILE = _DAEMON_DIR / "jobs.json"
_RUNS_DIR = _DAEMON_DIR / "runs"


def _ensure_dirs() -> None:
    """Create daemon directories if they do not exist."""
    _DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)


def _job_to_dict(job: AmbientJob) -> dict[str, Any]:
    """Serialize an AmbientJob to a JSON-compatible dict.

    Args:
        job: The job to serialize.

    Returns:
        A dict suitable for JSON serialization.
    """
    d = dataclasses.asdict(job)
    # Normalize enums to their string values
    d["last_status"] = job.last_status.value
    for trigger in d.get("triggers", []):
        if not isinstance(trigger["type"], str):
            trigger["type"] = str(trigger["type"])
    for output in d.get("outputs", []):
        if not isinstance(output["target"], str):
            output["target"] = str(output["target"])
    return d


def _job_from_dict(d: dict[str, Any]) -> AmbientJob:
    """Deserialize an AmbientJob from a dict.

    Args:
        d: The raw dict loaded from JSON.

    Returns:
        A reconstructed AmbientJob instance.
    """
    triggers = [
        TriggerConfig(
            type=TriggerType(t.get("type", "manual")),
            cron=t.get("cron", ""),
            interval_seconds=t.get("interval_seconds", 0),
            watch_patterns=t.get("watch_patterns", []),
            watch_dir=t.get("watch_dir", ""),
            debounce_seconds=t.get("debounce_seconds", 5.0),
            webhook_path=t.get("webhook_path", ""),
            webhook_secret=t.get("webhook_secret", ""),
            git_branch_pattern=t.get("git_branch_pattern", "*"),
        )
        for t in d.get("triggers", [])
    ]
    outputs = [
        OutputConfig(
            target=OutputTarget(o.get("target", "log")),
            file_path=o.get("file_path", ""),
            append=o.get("append", True),
            smtp_host=o.get("smtp_host", ""),
            smtp_port=o.get("smtp_port", 587),
            from_addr=o.get("from_addr", ""),
            to_addrs=o.get("to_addrs", []),
            subject_template=o.get("subject_template", "Bog Agents: {job_name} completed"),
            slack_webhook_url=o.get("slack_webhook_url", ""),
            slack_channel=o.get("slack_channel", ""),
            github_repo=o.get("github_repo", ""),
            github_issue_or_pr=o.get("github_issue_or_pr", 0),
            webhook_url=o.get("webhook_url", ""),
            webhook_headers=o.get("webhook_headers", {}),
        )
        for o in d.get("outputs", [])
    ]
    return AmbientJob(
        job_id=d.get("job_id", ""),
        name=d.get("name", ""),
        description=d.get("description", ""),
        prompt=d.get("prompt", ""),
        pipeline_name=d.get("pipeline_name", ""),
        skill_name=d.get("skill_name", ""),
        model=d.get("model", ""),
        working_dir=d.get("working_dir", ""),
        triggers=triggers,
        outputs=outputs,
        enabled=d.get("enabled", True),
        last_run_at=d.get("last_run_at", 0.0),
        last_status=JobStatus(d.get("last_status", "pending")),
        last_output=d.get("last_output", ""),
        run_count=d.get("run_count", 0),
        created_at=d.get("created_at", time.time()),
    )


def _run_to_dict(run: JobRun) -> dict[str, Any]:
    """Serialize a JobRun to a JSON-compatible dict.

    Args:
        run: The run to serialize.

    Returns:
        A dict suitable for JSON serialization.
    """
    d = dataclasses.asdict(run)
    d["status"] = run.status.value
    d["trigger_type"] = run.trigger_type.value
    return d


def _run_from_dict(d: dict[str, Any]) -> JobRun:
    """Deserialize a JobRun from a dict.

    Args:
        d: The raw dict loaded from JSON.

    Returns:
        A reconstructed JobRun instance.
    """
    return JobRun(
        run_id=d.get("run_id", ""),
        job_id=d.get("job_id", ""),
        job_name=d.get("job_name", ""),
        started_at=d.get("started_at", time.time()),
        finished_at=d.get("finished_at", 0.0),
        status=JobStatus(d.get("status", "running")),
        output=d.get("output", ""),
        error=d.get("error", ""),
        trigger_type=TriggerType(d.get("trigger_type", "manual")),
        trigger_context=d.get("trigger_context", {}),
    )


def load_jobs() -> list[AmbientJob]:
    """Load all jobs from the persistent store.

    Returns:
        List of AmbientJob instances; empty list if the store is missing or corrupt.
    """
    _ensure_dirs()
    if not _JOBS_FILE.exists():
        return []
    try:
        raw = _JOBS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            logger.warning("Jobs file is not a list, resetting: %s", _JOBS_FILE)
            return []
        return [_job_from_dict(item) for item in data]
    except Exception:
        logger.exception("Failed to load jobs from %s", _JOBS_FILE)
        return []


def save_jobs(jobs: list[AmbientJob]) -> None:
    """Atomically persist all jobs to disk.

    Args:
        jobs: The complete list of jobs to write.
    """
    _ensure_dirs()
    tmp_path = _JOBS_FILE.with_suffix(".json.tmp")
    data = [_job_to_dict(j) for j in jobs]
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    Path(tmp_path).replace(_JOBS_FILE)


def get_job(job_id: str) -> AmbientJob | None:
    """Look up a single job by ID.

    Args:
        job_id: The job identifier to search for.

    Returns:
        The matching AmbientJob, or None if not found.
    """
    jobs = load_jobs()
    for job in jobs:
        if job.job_id == job_id:
            return job
    return None


def upsert_job(job: AmbientJob) -> None:
    """Add a new job or replace an existing one with the same job_id.

    Args:
        job: The job to insert or update.
    """
    jobs = load_jobs()
    for i, existing in enumerate(jobs):
        if existing.job_id == job.job_id:
            jobs[i] = job
            save_jobs(jobs)
            return
    jobs.append(job)
    save_jobs(jobs)


def delete_job(job_id: str) -> bool:
    """Remove a job from the store by ID.

    Args:
        job_id: The identifier of the job to delete.

    Returns:
        True if a job was deleted, False if it was not found.
    """
    jobs = load_jobs()
    original_count = len(jobs)
    jobs = [j for j in jobs if j.job_id != job_id]
    if len(jobs) == original_count:
        return False
    save_jobs(jobs)
    return True


def save_run(run: JobRun) -> None:
    """Persist a job run record to disk.

    Args:
        run: The JobRun to save.
    """
    _ensure_dirs()
    run_file = _RUNS_DIR / f"{run.job_id}_{run.run_id}.json"
    run_file.write_text(json.dumps(_run_to_dict(run), indent=2), encoding="utf-8")


def list_runs(job_id: str | None = None, *, limit: int = 20) -> list[JobRun]:
    """Load and return job run records sorted by start time descending.

    Args:
        job_id: If provided, return only runs for this job. Otherwise return
            runs for all jobs.
        limit: Maximum number of runs to return.

    Returns:
        List of JobRun instances sorted newest-first.
    """
    _ensure_dirs()
    runs: list[JobRun] = []
    pattern = f"{job_id}_*.json" if job_id else "*.json"
    for run_file in _RUNS_DIR.glob(pattern):
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
            runs.append(_run_from_dict(data))
        except Exception:
            logger.debug("Could not read run file %s", run_file)
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs[:limit]
