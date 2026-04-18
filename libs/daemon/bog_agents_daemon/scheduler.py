"""Cron and interval scheduler for AmbientJob triggers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from bog_agents_daemon.models import AmbientJob, TriggerType

logger = logging.getLogger(__name__)


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
        """Return True when *value* is covered by a cron *field* expression."""
        if field == "*":
            return True
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


class DaemonScheduler:
    """Cron and interval scheduler for AmbientJob triggers.

    Polls the job store on each tick and dispatches due jobs via the provided
    runner callable.

    Attributes:
        _store_loader: Callable that returns the current list of AmbientJob.
        _runner: Async callable that executes a single job.
        _running_jobs: Set of job_ids currently being executed to prevent overlap.
    """

    def __init__(
        self,
        store_loader: Callable[[], list[AmbientJob]],
        runner: Callable[..., Coroutine[Any, Any, Any]],
    ) -> None:
        """Initialize the scheduler.

        Args:
            store_loader: Callable returning the current list of jobs from storage.
            runner: Async callable accepting an AmbientJob and keyword args,
                returning a JobRun.
        """
        self._store_loader = store_loader
        self._runner = runner
        self._running_jobs: set[str] = set()
        self._bg_tasks: set[asyncio.Task[None]] = set()

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

    async def _tick(self) -> None:
        """Process one scheduling tick: load jobs and fire any that are due."""
        jobs = self._store_loader()
        for job in jobs:
            if not job.enabled:
                continue
            if job.job_id in self._running_jobs:
                logger.debug("Job %s already running, skipping", job.job_id)
                continue
            due = await self._check_job_triggers(job)
            if due:
                task = asyncio.create_task(self._run_job_safely(job))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

    async def _run_job_safely(self, job: AmbientJob) -> None:
        """Execute a job, tracking running state to prevent concurrent runs.

        Args:
            job: The job to run.
        """
        self._running_jobs.add(job.job_id)
        try:
            await self._runner(job, trigger_type=TriggerType.CRON)
        except Exception:
            logger.exception("Job %s (%s) raised an unhandled exception", job.job_id, job.name)
        finally:
            self._running_jobs.discard(job.job_id)

    @staticmethod
    async def _check_job_triggers(job: AmbientJob) -> bool:
        """Check whether any CRON or INTERVAL trigger is currently due.

        Args:
            job: The job whose triggers to evaluate.

        Returns:
            True if at least one time-based trigger is due.
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
                    return True
            elif trigger.type == TriggerType.INTERVAL and trigger.interval_seconds > 0 and _is_interval_due(trigger.interval_seconds, job.last_run_at):
                logger.debug(
                    "Interval trigger due for job %s (%s): every %ds",
                    job.job_id,
                    job.name,
                    trigger.interval_seconds,
                )
                return True
        return False
