"""ROADMAP #55: thread-linked jobs, attempt caps, PR-scoped GitHub triggers, thread resume."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bog_agents_daemon import runner, store
from bog_agents_daemon.api import create_app
from bog_agents_daemon.models import AmbientJob, JobRun, JobStatus, TriggerConfig, TriggerType, github_trigger_matches, run_cap_reached
from bog_agents_daemon.scheduler import DaemonScheduler

_TOKEN = "t0k3n"


@pytest.fixture()
def client(tmp_daemon_dir: Path) -> TestClient:
    app = create_app(token=_TOKEN, scheduler=DaemonScheduler(store_loader=store.load_jobs, runner=AsyncMock()))
    return TestClient(app, raise_server_exceptions=True)


def _auth() -> dict[str, str]:
    return {"X-Daemon-Token": _TOKEN}


class TestModels:
    def test_cap_and_github_matching(self) -> None:
        job = AmbientJob(name="j", max_runs=2, run_count=1)
        assert not run_cap_reached(job)
        job.run_count = 2
        assert run_cap_reached(job)
        assert not run_cap_reached(AmbientJob(name="unlimited", run_count=99))
        scoped = TriggerConfig(type=TriggerType.GITHUB, github_number=42, github_kinds=["ci_failure"])
        assert github_trigger_matches(scoped, kind="ci_failure", number=42)
        assert not github_trigger_matches(scoped, kind="ci_failure", number=43)
        assert not github_trigger_matches(scoped, kind="issue_comment", number=42)
        assert github_trigger_matches(TriggerConfig(type=TriggerType.GITHUB), kind="anything", number=0)
        assert not github_trigger_matches(TriggerConfig(type=TriggerType.CRON, cron="* * * * *"), kind="ci_failure", number=42)

    def test_store_round_trip_and_auto_disable(self, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(
            name="sub",
            prompt="p",
            max_runs=1,
            thread_id="thread-1",
            checkpoint_db="/tmp/sessions.db",
            goal_ref="/repo/.bog-agents/goal.json",
            triggers=[TriggerConfig(type=TriggerType.GITHUB, github_number=7, github_kinds=["pr_comment"])],
        )
        store.upsert_job(job)
        loaded = store.get_job(job.job_id)
        assert loaded is not None
        assert (loaded.max_runs, loaded.thread_id, loaded.checkpoint_db, loaded.goal_ref) == (
            1,
            "thread-1",
            "/tmp/sessions.db",
            "/repo/.bog-agents/goal.json",
        )
        assert loaded.triggers[0].github_number == 7 and loaded.triggers[0].github_kinds == ["pr_comment"]
        store.record_run_result(job, last_run_at=1.0, last_status=JobStatus.COMPLETED, last_output="ok")
        after = store.get_job(job.job_id)
        assert after is not None and after.run_count == 1 and after.enabled is False  # cap of 1 spent → disabled


class TestScheduler:
    def test_dispatch_skips_a_capped_job(self, tmp_daemon_dir: Path) -> None:
        scheduler = DaemonScheduler(store_loader=store.load_jobs, runner=AsyncMock())
        job = AmbientJob(name="capped", prompt="p", max_runs=1, run_count=1)
        run = scheduler.dispatch(job, trigger_type=TriggerType.MANUAL)
        assert run.status == JobStatus.SKIPPED and "attempt cap" in run.error


class TestApi:
    def test_create_carries_thread_and_cap_and_patch_updates(self, client: TestClient) -> None:
        body = {
            "name": "pr-42",
            "prompt": "address review comments",
            "max_runs": 3,
            "thread_id": "thread-xyz",
            "goal_ref": "/repo/.bog-agents/goal.json",
            "triggers": [{"type": "github", "github_number": 42, "github_kinds": ["pr_review_comment", "ci_failure"]}],
        }
        created = client.post("/jobs", json=body, headers=_auth()).json()
        assert created["max_runs"] == 3 and created["thread_id"] == "thread-xyz"
        assert created["triggers"][0]["github_number"] == 42
        patched = client.patch(f"/jobs/{created['job_id']}", json={"max_runs": 5}, headers=_auth()).json()
        assert patched["max_runs"] == 5

    def test_github_webhook_only_fires_matching_number(self, client: TestClient) -> None:
        for number in (42, 43):
            client.post(
                "/jobs",
                json={"name": f"pr-{number}", "prompt": "p", "triggers": [{"type": "github", "github_number": number}]},
                headers=_auth(),
            )
        client.post("/jobs", json={"name": "any", "prompt": "p", "triggers": [{"type": "github"}]}, headers=_auth())
        payload = {
            "action": "created",
            "issue": {"number": 42, "title": "T", "body": "B", "pull_request": {}},
            "comment": {"body": "please fix", "user": {"login": "reviewer"}},
            "repository": {"full_name": "o/r"},
            "sender": {"login": "reviewer"},
        }
        with patch("bog_agents_daemon.scheduler.DaemonScheduler.dispatch") as dispatch:
            dispatch.return_value = JobRun(job_id="x", status=JobStatus.RUNNING)
            response = client.post("/webhooks/github", json=payload, headers={**_auth(), "X-GitHub-Event": "issue_comment"})
        assert response.status_code == 200
        names = sorted(call.args[0].name for call in dispatch.call_args_list)
        assert names == ["any", "pr-42"], names


class TestRunner:
    async def test_thread_job_reopens_checkpointer_and_frames_prompt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("langgraph.checkpoint.sqlite.aio")
        db = tmp_path / "sessions.db"
        db.write_bytes(b"")
        goal = tmp_path / "goal.json"
        goal.write_text('{"objective": "ship the PR"}', encoding="utf-8")
        job = AmbientJob(name="cont", prompt="handle the review", thread_id="thread-1", checkpoint_db=str(db), goal_ref=str(goal))
        captured: dict[str, Any] = {}

        class _Msg:
            type = "ai"
            content = "continued"

        def _fake_create_agent(**kwargs: Any) -> Any:
            captured.update(kwargs)
            agent = MagicMock()

            async def _astream(stream_input: Any, config: Any = None) -> Any:
                captured["input"] = stream_input
                captured["config"] = config
                yield {"node": {"messages": [_Msg()]}}

            agent.astream = _astream
            return agent

        with patch("bog_agents.create_agent", side_effect=_fake_create_agent):
            out = await runner._invoke_agent(job, "handle the review", trigger_type=TriggerType.GITHUB)
        assert out == "continued"
        assert captured["config"] == {"configurable": {"thread_id": "thread-1"}}
        assert type(captured["checkpointer"]).__name__ == "AsyncSqliteSaver"
        prompt = captured["input"]["messages"][0][1]
        assert prompt.startswith("[ambient: github trigger for job cont") and "Goal: ship the PR" in prompt and prompt.endswith("handle the review")

    async def test_missing_db_falls_back_to_fresh_run(self, tmp_path: Path) -> None:
        job = AmbientJob(name="cont", prompt="p", thread_id="thread-1", checkpoint_db=str(tmp_path / "missing.db"))
        assert runner.open_thread_checkpointer(job) is None
        assert runner.thread_checkpoint_db(AmbientJob(name="x", thread_id="t")).name == "sessions.db"
