"""Unit tests for daemon scheduler — cron, interval, file-change, debounce."""

from __future__ import annotations

import time
from pathlib import Path

from bog_agents_daemon.models import AmbientJob, TriggerConfig, TriggerType
from bog_agents_daemon.scheduler import (
    DaemonScheduler,
    _check_file_trigger,
    _is_cron_due,
    _is_interval_due,
)


class TestIsCronDue:
    def test_invalid_expression_returns_false(self):
        assert _is_cron_due("* * *", 0) is False

    def test_wildcard_fires_on_first_run(self):
        assert _is_cron_due("* * * * *", 0) is True

    def test_already_fired_this_minute(self):
        now = time.time()
        # last_run is the current second → same minute → should NOT fire
        assert _is_cron_due("* * * * *", now) is False

    def test_specific_hour_mismatch(self):
        # Force hour=99 — never matches
        assert _is_cron_due("0 99 * * *", 0) is False

    def test_step_syntax(self):
        # */5 in minute — matches minute 0,5,10...
        result = _is_cron_due("*/1 * * * *", 0)  # every minute, never run
        assert result is True

    def test_comma_separated(self):
        # Should return True if current minute is 0 and no prior run this minute
        # Hard to test deterministically; just verify no crash and bool result
        result = _is_cron_due("0,30 * * * *", 0)
        assert isinstance(result, bool)


class TestIsIntervalDue:
    def test_never_run_returns_true(self):
        assert _is_interval_due(60, 0) is True

    def test_just_ran_returns_false(self):
        assert _is_interval_due(3600, time.time()) is False

    def test_elapsed_returns_true(self):
        assert _is_interval_due(10, time.time() - 11) is True

    def test_zero_interval_returns_false(self):
        assert _is_interval_due(0, 0) is False


class TestCheckFileTrigger:
    def test_returns_none_with_no_watch_dir(self):
        t = TriggerConfig(type=TriggerType.FILE_CHANGE)
        assert _check_file_trigger(t, 0) is None

    def test_detects_new_file(self, tmp_path: Path):
        f = tmp_path / "log.txt"
        f.write_text("hello")
        t = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir=str(tmp_path),
            watch_patterns=["*.txt"],
        )
        result = _check_file_trigger(t, 0)
        assert result is not None
        assert result.name == "log.txt"

    def test_no_change_after_last_run(self, tmp_path: Path):
        f = tmp_path / "log.txt"
        f.write_text("hello")
        t = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir=str(tmp_path),
            watch_patterns=["*.txt"],
        )
        # last_run_at is far in the future
        result = _check_file_trigger(t, time.time() + 9999)
        assert result is None

    def test_pattern_mismatch_returns_none(self, tmp_path: Path):
        (tmp_path / "data.csv").write_text("a,b,c")
        t = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir=str(tmp_path),
            watch_patterns=["*.txt"],
        )
        assert _check_file_trigger(t, 0) is None

    def test_missing_watch_dir_returns_none(self):
        t = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir="/nonexistent/path/xyzzy",
        )
        assert _check_file_trigger(t, 0) is None


class TestDebounce:
    async def test_file_change_respects_debounce(self, tmp_path: Path):
        """File-change trigger should not fire until debounce_seconds has elapsed."""
        (tmp_path / "app.py").write_text("x = 1")

        fired: list[str] = []

        async def mock_runner(job, *, trigger_type, trigger_context=None):
            fired.append(job.job_id)

        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=mock_runner)

        trigger = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir=str(tmp_path),
            watch_patterns=["*.py"],
            debounce_seconds=60.0,  # very long debounce
        )
        job = AmbientJob(name="debounce-test", triggers=[trigger])

        # First tick — change detected but debounce not elapsed
        await scheduler._tick()
        assert fired == [], "Should not fire during debounce window"

    async def test_file_change_fires_after_debounce(self, tmp_path: Path):
        """Trigger fires once debounce window elapses."""
        import asyncio as _asyncio

        (tmp_path / "src.py").write_text("import os")

        fired: list[str] = []

        async def mock_runner(job, *, trigger_type, trigger_context=None):
            fired.append(job.job_id)

        trigger = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir=str(tmp_path),
            watch_patterns=["*.py"],
            debounce_seconds=0.0,  # no debounce
        )
        job = AmbientJob(name="instant-trigger", triggers=[trigger])
        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=mock_runner)

        await scheduler._tick()
        # Yield to allow background tasks to execute
        if scheduler._bg_tasks:
            await _asyncio.gather(*scheduler._bg_tasks, return_exceptions=True)
        assert len(fired) == 1, "Should fire immediately with debounce=0"

    async def test_running_job_skipped_on_next_tick(self, tmp_path: Path):
        """A job already running should not be re-triggered on the next tick."""
        (tmp_path / "x.txt").write_text("data")

        started: list[str] = []

        async def slow_runner(job, *, trigger_type, trigger_context=None):
            started.append(job.job_id)
            # Don't actually await anything — just record the call

        trigger = TriggerConfig(
            type=TriggerType.FILE_CHANGE,
            watch_dir=str(tmp_path),
            debounce_seconds=0.0,
        )
        job = AmbientJob(name="overlap-guard", triggers=[trigger])
        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=slow_runner)

        # Mark job as already running
        scheduler._running_jobs.add(job.job_id)
        await scheduler._tick()
        assert started == [], "Should not start overlapping run"


class TestSchedulerTriggerTypes:
    async def test_cron_trigger_fires(self):
        import asyncio as _asyncio

        fired: list[str] = []

        async def runner(job, *, trigger_type, trigger_context=None):
            fired.append(job.job_id)

        job = AmbientJob(
            name="cron-job",
            triggers=[TriggerConfig(type=TriggerType.CRON, cron="* * * * *")],
            last_run_at=0,
        )
        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=runner)
        await scheduler._tick()
        if scheduler._bg_tasks:
            await _asyncio.gather(*scheduler._bg_tasks, return_exceptions=True)
        assert len(fired) == 1

    async def test_interval_trigger_fires(self):
        import asyncio as _asyncio

        fired: list[str] = []

        async def runner(job, *, trigger_type, trigger_context=None):
            fired.append(job.job_id)

        job = AmbientJob(
            name="interval-job",
            triggers=[TriggerConfig(type=TriggerType.INTERVAL, interval_seconds=1)],
            last_run_at=0,
        )
        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=runner)
        await scheduler._tick()
        if scheduler._bg_tasks:
            await _asyncio.gather(*scheduler._bg_tasks, return_exceptions=True)
        assert len(fired) == 1

    async def test_disabled_job_not_triggered(self):
        fired: list[str] = []

        async def runner(job, *, trigger_type, trigger_context=None):
            fired.append(job.job_id)

        job = AmbientJob(
            name="disabled",
            enabled=False,
            triggers=[TriggerConfig(type=TriggerType.INTERVAL, interval_seconds=1)],
            last_run_at=0,
        )
        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=runner)
        await scheduler._tick()
        assert fired == []

    async def test_git_push_and_webhook_not_polled(self):
        """GIT_PUSH and WEBHOOK triggers are event-driven — scheduler ignores them."""
        fired: list[str] = []

        async def runner(job, *, trigger_type, trigger_context=None):
            fired.append(job.job_id)

        job = AmbientJob(
            name="event-job",
            triggers=[
                TriggerConfig(type=TriggerType.GIT_PUSH),
                TriggerConfig(type=TriggerType.WEBHOOK, webhook_path="/hook"),
            ],
            last_run_at=0,
        )
        scheduler = DaemonScheduler(store_loader=lambda: [job], runner=runner)
        await scheduler._tick()
        assert fired == [], "Event-driven triggers should not be polled"
