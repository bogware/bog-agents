"""ROADMAP #56: `POST /drain`, the draining scheduler and the run session registry."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from bog_agents_daemon import runner
from bog_agents_daemon.api import create_app
from bog_agents_daemon.models import AmbientJob, JobRun, JobStatus, TriggerType
from bog_agents_daemon.scheduler import DaemonScheduler
from bog_agents_daemon.store import load_jobs

_TOKEN = "drain-token"


def test_drain_endpoint_flips_health(tmp_daemon_dir: Path) -> None:
    scheduler = DaemonScheduler(store_loader=load_jobs, runner=AsyncMock())
    client = TestClient(create_app(token=_TOKEN, scheduler=scheduler), raise_server_exceptions=True)
    auth = {"X-Daemon-Token": _TOKEN}

    health = client.get("/health", headers=auth).json()
    assert health["running"] == 0 and health["draining"] is False

    resp = client.post("/drain", headers=auth)
    assert resp.status_code == 202
    assert resp.json() == {"status": "draining", "running": 0}
    assert client.get("/health", headers=auth).json()["draining"] is True
    assert client.post("/drain").status_code in (401, 403)


def test_draining_scheduler_starts_nothing(tmp_daemon_dir: Path) -> None:
    job = AmbientJob(name="nightly", prompt="do the thing")
    run_calls = AsyncMock()
    scheduler = DaemonScheduler(store_loader=lambda: [job], runner=run_calls)
    assert scheduler.begin_drain() == 0
    assert scheduler.draining

    skipped = scheduler.dispatch(job, trigger_type=TriggerType.MANUAL)
    assert skipped.status == JobStatus.SKIPPED and "draining" in skipped.error
    asyncio.run(scheduler._tick())
    run_calls.assert_not_awaited()
    assert scheduler.running_count == 0


def test_run_session_registry_round_trip(tmp_path: Path) -> None:
    registry = Path(os.environ["BOG_AGENTS_SESSIONS_DIR"])
    job = AmbientJob(name="nightly", prompt="p", working_dir=str(tmp_path), model="m", thread_id="t-9")
    run = JobRun(job_id=job.job_id, job_name=job.name)
    runner._register_run_session(job, run)
    written = registry / f"run-{run.run_id}.json"
    assert written.is_file()
    text = written.read_text(encoding="utf-8")
    assert '"kind": "daemon"' in text and '"thread_id": "t-9"' in text and '"state": "busy"' in text
    runner._unregister_run_session(run)
    assert not written.exists()
    runner._unregister_run_session(run)  # idempotent
