"""Persistent job store — JSON files in ~/.bog-agents/daemon/."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import subprocess
import sys
import threading
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

# Serialisation lock: prevents concurrent reads/writes to jobs.json from the
# scheduler tick and API request handlers running in the same process.
_jobs_lock = threading.Lock()

_DAEMON_DIR = Path.home() / ".bog-agents" / "daemon"
_JOBS_FILE = _DAEMON_DIR / "jobs.json"
_RUNS_DIR = _DAEMON_DIR / "runs"


def _secure_owner_only(path: Path) -> None:
    """Best-effort owner-only permissions on a secret-bearing file.

    jobs.json stores SMTP passwords, GitHub tokens, and webhook HMAC secrets
    in cleartext; at the default umask it lands world-readable (0644) on a
    shared/multi-user host. (REVIEW.md v2 P1-54.)

    chmod is effective on POSIX and a harmless no-op on Windows. The Windows
    ACL tightening (icacls) is applied ONLY to regular files — never to the
    containing directory, where `/inheritance:r` can strip the write access
    the daemon needs to create run files underneath it.
    """
    is_file = path.is_file()
    with contextlib.suppress(OSError):
        path.chmod(0o600 if is_file else 0o700)
    if sys.platform == "win32" and is_file:
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if user:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )


def spend_db_path() -> Path:
    """Path of the daemon's durable spend ledger (ROADMAP #51), beside `jobs.json`."""
    return _DAEMON_DIR / "spend.db"


def _ensure_dirs() -> None:
    """Create daemon directories if they do not exist."""
    _DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _secure_owner_only(_DAEMON_DIR)


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
            smtp_username=o.get("smtp_username", ""),
            smtp_password=o.get("smtp_password", ""),
            from_addr=o.get("from_addr", ""),
            to_addrs=o.get("to_addrs", []),
            subject_template=o.get("subject_template", "Bog Agents: {job_name} completed"),
            slack_webhook_url=o.get("slack_webhook_url", ""),
            slack_channel=o.get("slack_channel", ""),
            github_repo=o.get("github_repo", ""),
            github_issue_or_pr=o.get("github_issue_or_pr", 0),
            github_token=o.get("github_token", ""),
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
        max_retries=d.get("max_retries", 0),
        retry_backoff_seconds=d.get("retry_backoff_seconds", 2.0),
        budget_usd=d.get("budget_usd"),
        daily_ceiling_usd=d.get("daily_ceiling_usd"),
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
        attempts=d.get("attempts", 1),
        # P1-52: without this the field silently reset to [] on every disk
        # read-back, so dispatch failures vanished from `list_runs`.
        dispatch_errors=d.get("dispatch_errors", []),
    )


def _quarantine_corrupt_jobs(reason: str) -> None:
    """Move an unreadable jobs.json aside so the next save can't overwrite it.

    A parse failure used to return `[]`, and the very next `upsert_job`/
    `save_jobs` would replace the unreadable file with a fresh (near-empty)
    list — permanently destroying whatever jobs it held, with only a log
    line to show for it. Renaming the bad file to a timestamped sibling
    preserves the original bytes for manual recovery and makes the failure
    loud (ERROR, not WARNING). After the rename the path no longer exists,
    so subsequent loads simply start from an empty set rather than
    re-quarantining on every tick.

    Args:
        reason: Short description of why the file was rejected, for the log.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    quarantine = _JOBS_FILE.with_name(f"{_JOBS_FILE.name}.corrupt-{stamp}")
    # Guard against a same-second collision (the source is renamed away after
    # the first hit, so this is only reachable in pathological cases).
    counter = 1
    while quarantine.exists():
        quarantine = _JOBS_FILE.with_name(f"{_JOBS_FILE.name}.corrupt-{stamp}-{counter}")
        counter += 1
    try:
        _JOBS_FILE.replace(quarantine)
    except OSError:
        logger.exception("Failed to quarantine corrupt jobs file %s", _JOBS_FILE)
        return
    logger.error(
        "Jobs file %s was unreadable (%s); moved aside to %s. Starting with an "
        "empty job set — restore from the quarantine file or a backup to recover.",
        _JOBS_FILE,
        reason,
        quarantine,
    )


def _load_jobs_unlocked() -> list[AmbientJob]:
    """Load jobs without acquiring the lock (caller must hold _jobs_lock).

    Genuinely corrupt content (unparseable JSON, wrong top-level type, or an
    item that fails deserialization) is quarantined via
    `_quarantine_corrupt_jobs` so a later write can't clobber it. A transient
    `OSError` (a momentary lock or permission blip) is NOT quarantined — the
    file may be perfectly good — it just yields an empty list for this read.
    """
    _ensure_dirs()
    if not _JOBS_FILE.exists():
        return []
    try:
        raw = _JOBS_FILE.read_text(encoding="utf-8")
    except OSError:
        # Transient read failure — do not move the file; it may be fine.
        logger.exception("Failed to read jobs from %s", _JOBS_FILE)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        # Unparseable JSON (JSONDecodeError) — genuinely corrupt.
        logger.exception("Jobs file %s is not valid JSON", _JOBS_FILE)
        _quarantine_corrupt_jobs("invalid JSON")
        return []
    if not isinstance(data, list):
        logger.error("Jobs file %s is not a JSON list", _JOBS_FILE)
        _quarantine_corrupt_jobs("not a JSON list")
        return []
    try:
        return [_job_from_dict(item) for item in data]
    except Exception:
        logger.exception("Failed to deserialize jobs from %s", _JOBS_FILE)
        _quarantine_corrupt_jobs("deserialization error")
        return []


def _save_jobs_unlocked(jobs: list[AmbientJob]) -> None:
    """Persist jobs without acquiring the lock (caller must hold _jobs_lock).

    Durable atomic write: write into a sibling tmp file, fsync the file's
    contents to disk, then atomic-rename over the destination. Without
    the fsync a hard kill of the daemon between the write and the rename
    can leave the OS buffer cache holding the new bytes that never make
    it to disk — the symptom we observed when the daemon crashed mid-run
    and the freshly created job was missing on next start.
    """
    _ensure_dirs()
    tmp_path = _JOBS_FILE.with_suffix(".json.tmp")
    data = [_job_to_dict(j) for j in jobs]
    serialised = json.dumps(data, indent=2)
    # Open + write + flush + fsync in one go so the bytes are on disk
    # before the rename. Falls back gracefully on platforms that don't
    # support fsync on regular files (very rare).
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(serialised)
        f.flush()
        with contextlib.suppress(OSError, AttributeError):
            os.fsync(f.fileno())
    # Lock down the temp file BEFORE the rename so the secrets are never
    # briefly world-readable at the final path.
    _secure_owner_only(tmp_path)
    Path(tmp_path).replace(_JOBS_FILE)
    _secure_owner_only(_JOBS_FILE)


def load_jobs() -> list[AmbientJob]:
    """Load all jobs from the persistent store.

    Returns:
        List of AmbientJob instances; empty list if the store is missing or corrupt.
    """
    with _jobs_lock:
        return _load_jobs_unlocked()


def save_jobs(jobs: list[AmbientJob]) -> None:
    """Atomically persist all jobs to disk.

    Args:
        jobs: The complete list of jobs to write.
    """
    with _jobs_lock:
        _save_jobs_unlocked(jobs)


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

    The read-modify-write is performed under a single lock acquisition to
    prevent races between concurrent API requests and the scheduler tick.

    Args:
        job: The job to insert or update.
    """
    with _jobs_lock:
        jobs = _load_jobs_unlocked()
        for i, existing in enumerate(jobs):
            if existing.job_id == job.job_id:
                jobs[i] = job
                _save_jobs_unlocked(jobs)
                return
        jobs.append(job)
        _save_jobs_unlocked(jobs)


def record_run_result(
    job: AmbientJob,
    *,
    last_run_at: float,
    last_status: JobStatus,
    last_output: str,
) -> None:
    """Persist a job's run-state without clobbering concurrent config edits.

    The runner holds a job snapshot captured before the (possibly long) agent
    run. Writing that whole snapshot back via ``upsert_job`` would clobber any
    config edit (PATCH /jobs) that landed meanwhile — a lost update. This
    re-loads under the lock and, when the record already exists, merges ONLY
    the run-state fields, leaving config fields (prompt, triggers, outputs, …)
    intact. When the record does NOT exist (an ad-hoc/manual run of a job that
    was never registered), it inserts the provided job so the run is still
    recorded — matching the old upsert behaviour. (REVIEW.md v2 P1-56.)
    """
    with _jobs_lock:
        jobs = _load_jobs_unlocked()
        for existing in jobs:
            if existing.job_id == job.job_id:
                existing.last_run_at = last_run_at
                existing.last_status = last_status
                existing.last_output = last_output
                existing.run_count += 1
                _save_jobs_unlocked(jobs)
                return
        # Not previously registered — insert the snapshot (run-state already set).
        jobs.append(job)
        _save_jobs_unlocked(jobs)


def delete_job(job_id: str) -> bool:
    """Remove a job from the store by ID.

    The read-modify-write is performed under a single lock acquisition.

    Args:
        job_id: The identifier of the job to delete.

    Returns:
        True if a job was deleted, False if it was not found.
    """
    with _jobs_lock:
        jobs = _load_jobs_unlocked()
        original_count = len(jobs)
        jobs = [j for j in jobs if j.job_id != job_id]
        if len(jobs) == original_count:
            return False
        _save_jobs_unlocked(jobs)
        return True


_MAX_RUNS_PER_JOB = int(os.environ.get("BOG_DAEMON_MAX_RUNS_PER_JOB", "100"))


def _write_json_durable(path: Path, obj: Any) -> None:
    """Atomically write `obj` as pretty JSON to `path`, fsyncing before the rename.

    Shared by `save_run` and `reconcile_orphaned_runs`. Serialise first, write
    into a sibling `.tmp` file, fsync, then rename over the destination — the
    same durable-atomic pattern jobs.json uses (`_save_jobs_unlocked`). The
    previous in-place open+write meant a crash between truncate and flush left
    a half-written run record at its final path, which the loaders then
    skipped forever (DMN-10). On any failure the sibling tmp file is removed,
    so no partial file is ever left at or beside the destination. Falls back
    gracefully on the rare platform without `fsync` on regular files.

    Args:
        path: Destination file path (`.tmp` is appended for the scratch file).
        obj: JSON-serialisable object to persist.
    """
    serialised = json.dumps(obj, indent=2)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(serialised)
            f.flush()
            with contextlib.suppress(OSError, AttributeError):
                os.fsync(f.fileno())
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def save_run(run: JobRun) -> None:
    """Persist a job run record to disk, pruning old runs to cap disk usage.

    Keeps at most `BOG_DAEMON_MAX_RUNS_PER_JOB` (default 100) run files per job.
    Oldest runs by start time are deleted first.

    Args:
        run: The JobRun to save.
    """
    _ensure_dirs()
    run_file = _RUNS_DIR / f"{run.job_id}_{run.run_id}.json"
    _write_json_durable(run_file, _run_to_dict(run))
    _prune_runs(run.job_id)


def _prune_runs(job_id: str) -> None:
    """Delete the oldest run files for a job when the count exceeds the cap.

    Args:
        job_id: The job whose run files to prune.
    """
    run_files = sorted(_RUNS_DIR.glob(f"{job_id}_*.json"), key=lambda p: p.stat().st_mtime)
    excess = len(run_files) - _MAX_RUNS_PER_JOB
    for i in range(max(0, excess)):
        try:
            run_files[i].unlink(missing_ok=True)
        except Exception:
            logger.debug("Could not prune run file %s", run_files[i])


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
        except Exception as exc:
            # Loud, not debug: a corrupt/truncated run record silently
            # vanishing from /runs is exactly what DMN-10 was about.
            logger.warning("Skipping unreadable run file %s (corrupt or truncated?): %s", run_file, exc)
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs[:limit]


def reconcile_orphaned_runs() -> int:
    """Reconcile runs a crashed daemon left in the RUNNING state.

    `run_job` persists a run as RUNNING before invoking the (possibly long)
    agent and only rewrites it to COMPLETED/FAILED afterwards. If the daemon
    is killed mid-run, that record stays RUNNING forever — nothing ever
    reconciles it, so `/runs` and the CLI keep showing a run that will never
    finish. Call this once on startup: every run still marked RUNNING with no
    `finished_at` is stamped FAILED with an explanatory error and a
    `finished_at` of now, giving operators an honest terminal state.

    The on-disk dict is patched in place (rather than round-tripped through
    `JobRun`) so unknown/forward-compat fields survive.

    Returns:
        The number of orphaned runs reconciled.
    """
    _ensure_dirs()
    reconciled = 0
    note = "run interrupted: the daemon stopped or crashed mid-run; reconciled to FAILED on startup"
    for run_file in _RUNS_DIR.glob("*.json"):
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable run file %s during reconciliation (corrupt or truncated?): %s", run_file, exc)
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != JobStatus.RUNNING.value or data.get("finished_at"):
            continue
        data["status"] = JobStatus.FAILED.value
        data["finished_at"] = time.time()
        prior = data.get("error") or ""
        data["error"] = f"{prior}; {note}" if prior else note
        try:
            _write_json_durable(run_file, data)
            reconciled += 1
        except OSError:
            logger.warning("Could not rewrite orphaned run file %s", run_file)
    if reconciled:
        logger.info("Reconciled %d orphaned run(s) left RUNNING by a prior daemon", reconciled)
    return reconciled
