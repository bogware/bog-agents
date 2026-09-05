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

from croniter import croniter

from bog_agents_daemon.file_watch import FileWatchManager
from bog_agents_daemon.models import AmbientJob, JobRun, JobStatus, TriggerConfig, TriggerType, run_cap_reached

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
    """Check whether a cron trigger should fire now, catching up missed slots.

    Uses croniter for correct parsing (5-field standard, 6-field with seconds,
    ranges/steps/lists, and `@daily`-style macros) and, crucially, *catch-up*:
    if the daemon was down while a scheduled slot elapsed, the job fires once
    on the next tick rather than silently skipping to the following slot.
    (DMN-5/v4. Interval triggers already self-catch-up via elapsed time; cron
    did not — a daily 9am report simply vanished if the daemon was restarting
    across 9:00.)

    The rule: compute the first scheduled time strictly after the baseline
    (`last_run_at`, or one minute ago for a job that has never run). If that
    time has already passed, a slot is due — fire once. Because the baseline
    advances to the run's start time after each fire, a job that just ran is
    never immediately re-fired (dedup), and a multi-slot outage collapses to a
    single catch-up run rather than a backfill storm.

    All comparisons are in UTC, matching the daemon's prior behaviour. A
    malformed expression is logged and treated as not-due so one bad cron can't
    abort the scheduler tick and starve every later job. (REVIEW.md v2 P1-53.)

    Args:
        cron_expr: A cron expression (5-field, 6-field with seconds, or a macro
            such as `@hourly`).
        last_run_at: Unix timestamp of the last run (0/negative means never run).

    Returns:
        True if a scheduled slot is due, False otherwise.
    """
    if not croniter.is_valid(cron_expr):
        logger.warning("Invalid cron expression: %r", cron_expr)
        return False

    now = time.time()
    # Never run → baseline one minute back so a cron matching the current
    # minute fires immediately on the first tick (mirrors the old behaviour).
    base_ts = last_run_at if last_run_at > 0 else now - 60
    base_dt = datetime.fromtimestamp(base_ts, tz=UTC)
    try:
        next_dt = croniter(cron_expr, start_time=base_dt).get_next(datetime)
    except (ValueError, KeyError, OverflowError):
        # is_valid passed but resolution still failed — never crash the tick.
        logger.warning("Could not evaluate cron expression %r; treating as not due", cron_expr)
        return False
    return next_dt.timestamp() <= now


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
        # Event-driven file watching is only activated by the long-running
        # loop (run_forever); a bare _tick() never starts an observer thread,
        # keeping unit tests on the polling path with no thread leak.
        self._file_watcher = FileWatchManager()
        self._file_watching_active = False

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
        # Activate event-driven file watching for the lifetime of the loop; the
        # finally tears the observer thread down on shutdown/cancellation.
        self._file_watching_active = True
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    logger.info("Scheduler cancelled")
                    raise
                except Exception:
                    logger.exception("Scheduler tick error")
                await asyncio.sleep(tick_seconds)
        finally:
            self._file_watching_active = False
            self._file_watcher.stop()

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

        # Reconcile file-change observers to the current job set (only while the
        # long-running loop is active — see run_forever / __init__).
        if self._file_watching_active:
            watch_dirs = {
                trigger.watch_dir
                for job in jobs
                if job.enabled
                for trigger in job.triggers
                if trigger.type == TriggerType.FILE_CHANGE and trigger.watch_dir
            }
            self._file_watcher.sync(watch_dirs)

        for job in jobs:
            if not job.enabled or run_cap_reached(job):
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

    def dispatch(
        self,
        job: AmbientJob,
        *,
        trigger_type: TriggerType,
        trigger_context: dict[str, Any] | None = None,
        existing_run: JobRun | None = None,
    ) -> JobRun:
        """Reserve and launch a single job run under the scheduler's guards.

        This is the public entry point event-driven triggers (manual API run,
        git-push, webhook) must use instead of calling `run_job` directly. It
        funnels the run through the same `_running_jobs` overlap guard and
        `_semaphore` concurrency bound the polled scheduler uses, so an
        external push/webhook storm cannot fan out unbounded parallel runs of
        the same job against one workspace/history.

        Overlap policy is **skip-if-running**: if the job is already in
        `_running_jobs`, no new task is launched. A warning is logged and the
        caller's placeholder run (or a freshly minted one) is returned with
        `status=SKIPPED` so HTTP clients still observe a coherent run record.

        The reservation is performed synchronously (before the awaitable task
        is scheduled) so two near-simultaneous dispatches of the same job
        cannot both pass the running-check and double-fire.

        Args:
            job: The job to run.
            trigger_type: How this execution was initiated.
            trigger_context: Optional metadata from the trigger.
            existing_run: An already-persisted placeholder JobRun to reuse so
                HTTP clients see a stable run_id immediately. When omitted, a
                minimal placeholder is created in-memory for the return value
                only (the runner allocates and persists the real record).

        Returns:
            The JobRun the caller should return to its client: either the
            launched placeholder (status unchanged) or, when skipped, a run
            marked `status=SKIPPED`.
        """
        if run_cap_reached(job):
            # ROADMAP #55: the attempt cap is spent; record a skipped run so the
            # caller sees why nothing happened.
            capped = existing_run or JobRun(
                job_id=job.job_id,
                job_name=job.name,
                trigger_type=trigger_type,
                trigger_context=trigger_context or {},
            )
            capped.status = JobStatus.SKIPPED
            capped.error = f"attempt cap reached ({job.run_count}/{job.max_runs})"
            return capped
        # Skip-if-running: a job already executing must not be double-fired by
        # an event trigger. Reserve the slot synchronously so a second dispatch
        # arriving in the same tick is rejected too.
        if job.job_id in self._running_jobs:
            logger.warning(
                "Job %s (%s) is already running; skipping overlapping %s trigger",
                job.job_id,
                job.name,
                trigger_type.value,
            )
            skipped = existing_run or JobRun(
                job_id=job.job_id,
                job_name=job.name,
                trigger_type=trigger_type,
                trigger_context=trigger_context or {},
            )
            skipped.status = JobStatus.SKIPPED
            return skipped

        self._running_jobs.add(job.job_id)
        run = existing_run or JobRun(
            job_id=job.job_id,
            job_name=job.name,
            trigger_type=trigger_type,
            trigger_context=trigger_context or {},
        )
        task = asyncio.create_task(
            self._run_job_safely(
                job,
                trigger_type=trigger_type,
                trigger_context=trigger_context,
                existing_run=existing_run,
            )
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return run

    async def _run_job_safely(
        self,
        job: AmbientJob,
        *,
        trigger_type: TriggerType = TriggerType.CRON,
        trigger_context: dict[str, Any] | None = None,
        existing_run: JobRun | None = None,
    ) -> None:
        """Execute a job under the concurrency semaphore, preventing parallel runs.

        Args:
            job: The job to run.
            trigger_type: How this execution was initiated.
            trigger_context: Optional metadata from the trigger.
            existing_run: When set, a placeholder JobRun the API already
                persisted is forwarded to the runner so it reuses that record
                instead of allocating a fresh one.
        """
        try:
            async with self._semaphore:
                # _running_jobs membership was already added by _tick/dispatch
                # before task creation; we re-assert here to handle any path
                # that invokes this method directly without that reservation.
                self._running_jobs.add(job.job_id)
                try:
                    runner_kwargs: dict[str, Any] = {
                        "trigger_type": trigger_type,
                        "trigger_context": trigger_context,
                    }
                    if existing_run is not None:
                        runner_kwargs["_existing_run"] = existing_run
                    await self._runner(job, **runner_kwargs)
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
                changed_path = self._detect_file_change(trigger, job.last_run_at)
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

    def _detect_file_change(self, trigger: TriggerConfig, last_run_at: float) -> Path | None:
        """Detect a matching file change, preferring watchdog events over polling.

        A first run (`last_run_at <= 0`) or a directory with no live observer
        walks the tree via `_check_file_trigger`, so pre-existing files are seen
        and unwatchable directories still work. Once the daemon is running and a
        directory is being watched, subsequent runs consult the observer's
        recorded events instead — firing within OS event latency and costing
        nothing per tick.

        Args:
            trigger: The FILE_CHANGE trigger to evaluate.
            last_run_at: Unix timestamp of the job's last run (0 = never run).

        Returns:
            The changed path that should fire the trigger, or None.
        """
        watch_dir = trigger.watch_dir
        if not watch_dir:
            return None
        if last_run_at <= 0 or not self._file_watcher.is_watching(watch_dir):
            return _check_file_trigger(trigger, last_run_at)
        return self._file_watcher.changed_since(watch_dir, trigger.watch_patterns, last_run_at)


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
