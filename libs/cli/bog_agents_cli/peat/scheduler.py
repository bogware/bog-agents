"""Peat in-process scheduler.

Lives inside the running CLI. Keeps a list of :class:`PeatJob` and an
asyncio task that wakes up every ``tick_interval_s`` seconds to check if
anything is due to fire.

When a job fires, the scheduler hands off to a *runner callback* the
caller supplied (the CLI passes in a function that spawns the Peat
sub-agent and returns a :class:`PeatJobRun`). The scheduler is otherwise
oblivious to what runs — it just times things and writes the inbox.

When the CLI shuts down, ``stop()`` cancels the loop and waits for any
in-flight job to finish (subject to its own ``timeout_s``).

**Cron parsing.** ``croniter`` is already a dependency. We use it for
five-field crons. We also accept ``@once @ <ISO-8601>`` for one-shot
jobs and ``@every <N>m|h|d`` as a friendlier fixed-interval shorthand.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from bog_agents_cli.peat.jobs import (
    PeatJob,
    PeatJobRun,
    jobs_dir,
    list_jobs,
    save_job,
)

logger = logging.getLogger(__name__)


# Runner callback: the scheduler hands the job to the caller, who spawns
# Peat and runs the prompt, then returns a completed PeatJobRun.
RunnerFn = Callable[[PeatJob], Awaitable[PeatJobRun]]


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------


_EVERY_RE = re.compile(r"^@every\s+(\d+)\s*([smhd])$", re.IGNORECASE)
_ONCE_RE = re.compile(r"^@once\s+@\s*(.+)$", re.IGNORECASE)


def next_fire_time(schedule: str, *, after: float | None = None) -> float | None:
    """Return the next firing time (unix seconds) for ``schedule``.

    Supported forms:

    - ``"0 9 * * 1-5"`` (5-field cron, parsed by croniter)
    - ``"@every 30m"`` (every 30 minutes; ``s|m|h|d`` units accepted)
    - ``"@once @ 2026-05-04T09:00:00Z"`` (one-shot at an ISO-8601 instant)
    - empty string → manual-only (returns None)

    Args:
        schedule: Schedule string.
        after: Compute the next fire time strictly after this unix
            timestamp. Defaults to ``time.time()``.

    Returns:
        Unix seconds of the next fire, or ``None`` if the schedule is
        empty, malformed, or already in the past for a one-shot.
    """
    schedule = (schedule or "").strip()
    if not schedule:
        return None
    base = after if after is not None else time.time()

    m = _EVERY_RE.match(schedule)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
        return base + seconds

    m = _ONCE_RE.match(schedule)
    if m:
        ts_str = m.group(1).strip()
        try:
            # Accept Z suffix and offset-naive (treat as UTC).
            normalized = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ts = dt.timestamp()
        except ValueError:
            logger.warning("peat: malformed @once timestamp %r", ts_str)
            return None
        return ts if ts > base else None

    # Five-field cron via croniter.
    try:
        from croniter import croniter
    except ImportError:
        logger.warning("peat: croniter not installed; cron schedules disabled")
        return None
    try:
        it = croniter(schedule, datetime.fromtimestamp(base, tz=UTC))
        return float(it.get_next(float))
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("peat: invalid cron %r: %s", schedule, exc)
        return None


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def inbox_path(config_dir: Path) -> Path:
    return config_dir / "peat" / "inbox.json"


def append_inbox(config_dir: Path, entry: dict) -> None:
    """Append ``entry`` to ``inbox.json``. Resilient to corruption.

    The inbox is a small JSON list. If the file is missing, malformed, or
    over a sane size cap (1 MB), we start a fresh list rather than
    crashing the scheduler.
    """
    path = inbox_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    if path.exists():
        try:
            raw = path.read_bytes()
            if len(raw) <= 1 * 1024 * 1024:
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, list):
                    items = parsed
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("peat inbox: resetting corrupt %s: %s", path, exc)
            items = []
    items.append(entry)
    # Cap inbox size so a runaway scheduler can't fill the user's disk.
    if len(items) > 500:
        items = items[-500:]
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


def read_inbox(config_dir: Path) -> list[dict]:
    """Read the inbox JSON. Returns [] if missing or corrupt."""
    path = inbox_path(config_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("peat inbox: failed to read %s: %s", path, exc)
        return []
    return data if isinstance(data, list) else []


def clear_inbox(config_dir: Path) -> int:
    """Empty the inbox. Returns the count of dropped entries."""
    items = read_inbox(config_dir)
    inbox_path(config_dir).write_text("[]", encoding="utf-8")
    return len(items)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class PeatScheduler:
    """In-process job scheduler. Lives for the duration of the CLI session.

    Usage::

        scheduler = PeatScheduler(config_dir, runner=run_peat_job)
        await scheduler.start()
        ...
        await scheduler.stop()

    The scheduler is *single-fire-at-a-time* per job by default. Setting
    ``concurrent: true`` on a job opts in to overlapping fires.
    """

    def __init__(
        self,
        config_dir: Path,
        *,
        runner: RunnerFn,
        tick_interval_s: float = 30.0,
    ) -> None:
        self._config_dir = config_dir
        self._runner = runner
        # Cap tick floor at 0.05s so tests can drive the scheduler quickly
        # while still preventing a busy-loop. In production callers pass
        # the default 30s.
        self._tick = max(0.05, tick_interval_s)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._inflight: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """Begin the scheduler loop. Idempotent."""
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="peat-scheduler")
        logger.info("peat scheduler started (tick=%ds)", self._tick)

    async def stop(self) -> None:
        """Stop the scheduler. Waits for in-flight runs (with their own timeouts)."""
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        # Drain in-flight runs.
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        self._task = None
        logger.info("peat scheduler stopped")

    async def _loop(self) -> None:
        # Initial pass: compute next_fire_at for any jobs missing it.
        self._refresh_next_fire_for_all()
        while not self._stopping.is_set():
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("peat scheduler tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _tick_once(self) -> None:
        now = time.time()
        for job in list_jobs(self._config_dir):
            if not job.enabled:
                continue
            if not job.schedule:
                continue
            if job.next_fire_at <= 0:
                # Compute on first sight.
                nxt = next_fire_time(job.schedule, after=now)
                if nxt is not None:
                    job.next_fire_at = nxt
                    save_job(self._config_dir, job)
                continue
            if job.next_fire_at > now:
                continue
            # Job is due.
            if not job.concurrent and job.job_id in self._inflight:
                logger.debug("peat: skipping fire of %s — previous still running", job.job_id)
                continue
            self._inflight[job.job_id] = asyncio.create_task(
                self._fire(job), name=f"peat-job-{job.job_id}"
            )

    def _refresh_next_fire_for_all(self) -> None:
        # Only initialize jobs that have *never* had a next_fire_at set
        # (value 0). A past-time value is intentional — it means "fire
        # ASAP" — and must be preserved so the next tick picks it up.
        now = time.time()
        for job in list_jobs(self._config_dir):
            if not job.enabled or not job.schedule:
                continue
            if job.next_fire_at == 0:
                nxt = next_fire_time(job.schedule, after=now)
                if nxt is not None:
                    job.next_fire_at = nxt
                    save_job(self._config_dir, job)

    async def _fire(self, job: PeatJob) -> None:
        started = time.time()
        try:
            run = await asyncio.wait_for(self._runner(job), timeout=job.timeout_s)
        except TimeoutError:
            run = PeatJobRun(
                job_id=job.job_id,
                run_id=f"run-{int(started)}",
                started_at=started,
                duration_s=time.time() - started,
                status="timeout",
                error=f"timed out after {job.timeout_s}s",
            )
        except Exception as exc:
            logger.exception("peat job %s runner crashed", job.job_id)
            run = PeatJobRun(
                job_id=job.job_id,
                run_id=f"run-{int(started)}",
                started_at=started,
                duration_s=time.time() - started,
                status="fail",
                error=f"runner exception: {exc.__class__.__name__}: {exc}",
            )
        finally:
            self._inflight.pop(job.job_id, None)

        # Update job state.
        job.last_fired_at = run.started_at
        job.run_count += 1
        if run.status != "ok":
            job.consecutive_failures += 1
        else:
            job.consecutive_failures = 0
        # Auto-disable on too many failures (when the user asked for it).
        if job.on_failure == "disable" and job.consecutive_failures >= 3:
            job.enabled = False
            logger.warning("peat: auto-disabled job %s after 3 consecutive failures", job.job_id)
        # Compute next fire (if recurring).
        nxt = next_fire_time(job.schedule, after=time.time())
        job.next_fire_at = nxt or 0.0
        save_job(self._config_dir, job)

        # Notify inbox.
        if job.notify_inbox and (run.status != "fail" or job.on_failure != "silent"):
            append_inbox(
                self._config_dir,
                {
                    "when": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(run.started_at)),
                    "job_id": job.job_id,
                    "job_name": job.name or job.job_id,
                    "status": run.status,
                    "summary": run.summary or run.error,
                    "output_path": run.output_path,
                    "duration_s": round(run.duration_s, 2),
                },
            )
        logger.info("peat job %s fired: status=%s in %.1fs", job.job_id, run.status, run.duration_s)


__all__ = [
    "PeatScheduler",
    "RunnerFn",
    "append_inbox",
    "clear_inbox",
    "inbox_path",
    "jobs_dir",
    "next_fire_time",
    "read_inbox",
]
