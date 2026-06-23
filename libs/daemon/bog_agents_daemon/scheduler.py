"""Cron and interval scheduler for AmbientJob triggers."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bog_agents_daemon.models import AmbientJob, TriggerConfig, TriggerType

logger = logging.getLogger(__name__)

# Directories never worth walking for a file-change trigger. Pruning these in
# place keeps a tick from descending into multi-gigabyte vendored/VCS trees.
_FILE_TRIGGER_PRUNE_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache"})

# Hard cap on the number of files stat()'d per tick. A FILE_CHANGE trigger
# runs on every scheduler tick (default ~30s); without a bound a large
# watch_dir makes a single tick exceed the interval and starve cron/interval
# jobs. When the cap is hit we log and bail rather than blocking the loop.
_FILE_TRIGGER_MAX_FILES = 50_000


def _is_cron_due(cron_expr: str, last_run_at: float) -> bool:
    """Check whether a 5-field cron expression would have fired since last_run_at.

    Implements a simple 5-field check (minute hour day month weekday) against
    the current UTC time without external dependencies. For each enabled field,
    checks whether the current time unit matches the expression and whether the
    expression would have fired in the interval since the last run.

    Supported syntax: ``*``, single values, comma-separated lists, and
    ``*/step`` syntax. Ranges (``1-5``) are also supported.

    Args:
        cron_expr: A 5-field cron expression string.
        last_run_at: Unix timestamp of the last run (0 means never run).

    Returns:
        True if the cron expression is due to fire, False otherwise.
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        logger.warning("Invalid cron expression (expected 5 fields): %r", cron_expr)
        return False

    now = datetime.now(tz=UTC)
    # If we have never run, use a point 60 seconds before now as the baseline
    last_dt = datetime.fromtimestamp(last_run_at, tz=UTC) if last_run_at > 0 else None

    minute_field, hour_field, dom_field, month_field, dow_field = fields

    def _matches_field(field: str, value: int, min_val: int) -> bool:
        """Return True when *value* is covered by a cron *field* expression.

        A malformed field (non-integer step/range/value) is treated as
        'not matching' rather than raising — a single bad cron must not abort
        the scheduler tick and starve every later job. (REVIEW.md v2 P1-53.)
        """
        if field == "*":
            return True
        try:
            for part in field.split(","):
                if "/" in part:
                    base, step_str = part.split("/", 1)
                    step = int(step_str)
                    base_val = min_val if base == "*" else int(base)
                    if step > 0 and (value - base_val) >= 0 and (value - base_val) % step == 0:
                        return True
                elif "-" in part:
                    lo, hi = part.split("-", 1)
                    if int(lo) <= value <= int(hi):
                        return True
                elif value == int(part):
                    return True
        except ValueError:
            logger.warning("Unparsable cron field %r in %r — treating as not due", field, cron_expr)
            return False
        return False

    # Check if current time matches the cron fields
    if not _matches_field(month_field, now.month, 1):
        return False
    if not _matches_field(dom_field, now.day, 1):
        return False
    # weekday: cron uses 0=Sunday..6=Saturday; Python isoweekday 1=Mon..7=Sun
    iso_wd = now.isoweekday()  # 1=Mon..7=Sun
    cron_wd = iso_wd % 7  # 0=Sun..6=Sat
    if not _matches_field(dow_field, cron_wd, 0):
        return False
    if not _matches_field(hour_field, now.hour, 0):
        return False
    if not _matches_field(minute_field, now.minute, 0):
        return False

    # The fields match the current time. Now check we haven't already fired
    # this minute (last_run was within the current minute).
    if last_dt is not None:
        # Truncate both timestamps to the current minute
        now_minute_ts = now.replace(second=0, microsecond=0).timestamp()
        last_minute_ts = last_dt.replace(second=0, microsecond=0).timestamp()
        if last_minute_ts >= now_minute_ts:
            # Already fired this minute
            return False

    return True


def _is_interval_due(interval_seconds: int, last_run_at: float) -> bool:
    """Check whether an interval trigger is due.

    Args:
        interval_seconds: The minimum number of seconds between runs.
        last_run_at: Unix timestamp of the last run (0 means never run).

    Returns:
        True if at least `interval_seconds` have elapsed since the last run.
    """
    if interval_seconds <= 0:
        return False
    elapsed = time.time() - last_run_at
    return elapsed >= interval_seconds


_MAX_CONCURRENT_JOBS = int(os.environ.get("BOG_DAEMON_MAX_CONCURRENT_JOBS", "5"))


class DaemonScheduler:
    """Cron and interval scheduler for AmbientJob triggers.

    Polls the job store on each tick and dispatches due jobs via the provided
    runner callable.

    Attributes:
        _store_loader: Callable that returns the current list of AmbientJob.
        _runner: Async callable that executes a single job.
        _running_jobs: Set of job_ids currently being executed to prevent overlap.
        _semaphore: Limits total concurrent agent executions.
    """

    def __init__(
        self,
        store_loader: Callable[[], list[AmbientJob]],
        runner: Callable[..., Coroutine[Any, Any, Any]],
        *,
        max_concurrent: int = _MAX_CONCURRENT_JOBS,
    ) -> None:
        """Initialize the scheduler.

        Args:
            store_loader: Callable returning the current list of jobs from storage.
            runner: Async callable accepting an AmbientJob and keyword args,
                returning a JobRun.
            max_concurrent: Maximum number of jobs that may run simultaneously.
        """
        self._store_loader = store_loader
        self._runner = runner
        self._running_jobs: set[str] = set()
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        # Maps job_id -> unix timestamp when a file-change was first detected,
        # used to implement per-trigger debounce_seconds.
        self._file_change_pending: dict[str, float] = {}

    async def run_forever(self, *, tick_seconds: float = 30) -> None:
        """Run the scheduling loop indefinitely.

        On each tick, loads all jobs from storage, checks their triggers, and
        dispatches any that are due. Runs until cancelled.

        Args:
            tick_seconds: Seconds to sleep between scheduling checks.

        Raises:
            asyncio.CancelledError: When the task is cancelled.
        """
        logger.info("Scheduler started (tick=%.0fs)", tick_seconds)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                logger.info("Scheduler cancelled")
                raise
            except Exception:
                logger.exception("Scheduler tick error")
            await asyncio.sleep(tick_seconds)

    def reload_jobs(self) -> list[AmbientJob]:
        """Force the scheduler to re-read jobs from the store.

        Invoked from SIGHUP. The current scheduler always reloads on each
        tick, so this is mostly a hook for future caching plus a place to
        garbage-collect debounce state belonging to jobs that no longer exist.
        """
        jobs = self._store_loader()
        live_ids = {job.job_id for job in jobs}
        stale = [jid for jid in self._file_change_pending if jid not in live_ids]
        for jid in stale:
            self._file_change_pending.pop(jid, None)
        if stale:
            logger.debug("Cleared file-change debounce state for %d deleted job(s)", len(stale))
        return jobs

    async def _tick(self) -> None:
        """Process one scheduling tick: load jobs and fire any that are due."""
        jobs = self._store_loader()

        # Garbage-collect debounce entries for jobs that have been deleted.
        live_ids = {job.job_id for job in jobs}
        for jid in list(self._file_change_pending):
            if jid not in live_ids:
                self._file_change_pending.pop(jid, None)

        for job in jobs:
            if not job.enabled:
                continue
            if job.job_id in self._running_jobs:
                logger.debug("Job %s already running, skipping", job.job_id)
                continue
            trigger_type, trigger_context = await self._check_job_triggers(job)
            if trigger_type is None:
                continue
            # Reserve the running-jobs slot synchronously (before awaiting the
            # task creation) so a second tick or webhook arriving right now
            # cannot also pass the duplicate-check above and double-fire.
            self._running_jobs.add(job.job_id)
            task = asyncio.create_task(self._run_job_safely(job, trigger_type=trigger_type, trigger_context=trigger_context))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _run_job_safely(
        self,
        job: AmbientJob,
        *,
        trigger_type: TriggerType = TriggerType.CRON,
        trigger_context: dict[str, Any] | None = None,
    ) -> None:
        """Execute a job under the concurrency semaphore, preventing parallel runs.

        Args:
            job: The job to run.
            trigger_type: How this execution was initiated.
            trigger_context: Optional metadata from the trigger.
        """
        try:
            async with self._semaphore:
                # _running_jobs membership was already added by _tick before
                # task creation; we re-assert here to handle the API path
                # (where this method is invoked directly without _tick).
                self._running_jobs.add(job.job_id)
                try:
                    await self._runner(job, trigger_type=trigger_type, trigger_context=trigger_context)
                except Exception:
                    logger.exception("Job %s (%s) raised an unhandled exception", job.job_id, job.name)
        finally:
            self._running_jobs.discard(job.job_id)

    async def _check_job_triggers(self, job: AmbientJob) -> tuple[TriggerType | None, dict[str, Any] | None]:
        """Check whether any trigger is currently due.

        Evaluates CRON, INTERVAL, and FILE_CHANGE triggers in order, returning
        on the first one that fires. FILE_CHANGE triggers respect the configured
        `debounce_seconds`: the trigger only fires when the same change has been
        continuously detected for at least that many seconds.

        GIT_PUSH and WEBHOOK triggers are event-driven (fired by the API) and
        are not polled here.

        Args:
            job: The job whose triggers to evaluate.

        Returns:
            A tuple of (TriggerType, trigger_context) if due, or (None, None).
        """
        for trigger in job.triggers:
            if trigger.type == TriggerType.CRON and trigger.cron:
                if _is_cron_due(trigger.cron, job.last_run_at):
                    logger.debug(
                        "Cron trigger due for job %s (%s): %s",
                        job.job_id,
                        job.name,
                        trigger.cron,
                    )
                    return TriggerType.CRON, None

            elif trigger.type == TriggerType.INTERVAL and trigger.interval_seconds > 0:
                if _is_interval_due(trigger.interval_seconds, job.last_run_at):
                    logger.debug(
                        "Interval trigger due for job %s (%s): every %ds",
                        job.job_id,
                        job.name,
                        trigger.interval_seconds,
                    )
                    return TriggerType.INTERVAL, None

            elif trigger.type == TriggerType.FILE_CHANGE:
                changed_path = _check_file_trigger(trigger, job.last_run_at)
                if changed_path is not None:
                    debounce = max(0.0, trigger.debounce_seconds)
                    if debounce > 0:
                        # Record first detection time; only fire after debounce window
                        first_seen = self._file_change_pending.setdefault(job.job_id, time.time())
                        if time.time() - first_seen < debounce:
                            logger.debug(
                                "File-change debouncing job %s (%s): %s, %.1fs remaining",
                                job.job_id,
                                job.name,
                                changed_path,
                                debounce - (time.time() - first_seen),
                            )
                            continue
                    # Debounce elapsed (or not configured) — clear pending and fire
                    self._file_change_pending.pop(job.job_id, None)
                    logger.debug(
                        "File-change trigger fired for job %s (%s): %s",
                        job.job_id,
                        job.name,
                        changed_path,
                    )
                    return TriggerType.FILE_CHANGE, {
                        "trigger_path": str(changed_path),
                        "trigger_type": "file_change",
                    }
                else:
                    # No change detected — reset debounce state
                    self._file_change_pending.pop(job.job_id, None)

        return None, None


def _check_file_trigger(trigger: TriggerConfig, last_run_at: float) -> Path | None:
    """Check whether any watched file has been modified since the last run.

    Iterates over files under `trigger.watch_dir` (recursively) and matches
    filenames against each pattern in `trigger.watch_patterns` using
    `fnmatch`. Returns the first path whose mtime is newer than `last_run_at`.

    Heavy/irrelevant directories (`.git`, `node_modules`, `.venv`, build
    caches — see `_FILE_TRIGGER_PRUNE_DIRS`) are pruned in place, and the
    number of files examined per tick is capped at `_FILE_TRIGGER_MAX_FILES`
    so a large watch_dir can't make a single tick overrun the scheduler
    interval and starve cron/interval jobs.

    If `last_run_at` is 0 (job has never run), every matched file is considered
    changed so the trigger fires immediately on the first tick.

    Args:
        trigger: The FILE_CHANGE trigger configuration.
        last_run_at: Unix timestamp of the last job run (0 = never run).

    Returns:
        The Path of the first modified file that matches the patterns, or None.
    """
    watch_dir = trigger.watch_dir
    if not watch_dir:
        return None

    patterns = trigger.watch_patterns or ["*"]

    try:
        root = Path(watch_dir)
        if not root.is_dir():
            return None

        examined = 0
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune in place so os.walk never descends into these subtrees.
            dirnames[:] = [d for d in dirnames if d not in _FILE_TRIGGER_PRUNE_DIRS]
            for filename in filenames:
                examined += 1
                if examined > _FILE_TRIGGER_MAX_FILES:
                    logger.warning(
                        "File-change trigger: watch_dir %s exceeds %d files; "
                        "stopping scan early this tick. Narrow watch_patterns or watch a smaller dir.",
                        watch_dir,
                        _FILE_TRIGGER_MAX_FILES,
                    )
                    return None
                if not any(fnmatch.fnmatch(filename, pat) for pat in patterns):
                    continue
                full_path = Path(dirpath) / filename
                try:
                    mtime = full_path.stat().st_mtime
                except OSError:
                    continue
                if last_run_at == 0 or mtime > last_run_at:
                    return full_path
    except OSError:
        logger.debug("File-change trigger: error walking %s", watch_dir)

    return None
