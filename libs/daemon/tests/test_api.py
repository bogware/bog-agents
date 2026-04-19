"""Integration tests for the daemon REST API (FastAPI endpoints)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from bog_agents_daemon.api import create_app
from bog_agents_daemon.models import AmbientJob, JobRun, JobStatus, TriggerType
from bog_agents_daemon.scheduler import DaemonScheduler
from bog_agents_daemon.store import load_jobs, upsert_job

_TEST_TOKEN = "test-token-abc123"


@pytest.fixture()
def scheduler():
    return DaemonScheduler(store_loader=load_jobs, runner=AsyncMock())


@pytest.fixture()
def client(tmp_daemon_dir: Path, scheduler: DaemonScheduler) -> TestClient:
    app = create_app(token=_TEST_TOKEN, scheduler=scheduler)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def auth() -> dict[str, str]:
    return {"X-Daemon-Token": _TEST_TOKEN}


# ---------------------------------------------------------------------------
# /ready  (no auth)
# ---------------------------------------------------------------------------


class TestReady:
    def test_ready_no_auth(self, client: TestClient) -> None:
        resp = client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_ok(self, client: TestClient, auth: dict) -> None:
        resp = client.get("/health", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "job_count" in data

    def test_health_no_token_401(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 401

    def test_health_wrong_token_401(self, client: TestClient) -> None:
        assert client.get("/health", headers={"X-Daemon-Token": "wrong"}).status_code == 401


# ---------------------------------------------------------------------------
# POST /jobs — create
# ---------------------------------------------------------------------------


class TestCreateJob:
    def test_create_minimal_job(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        payload: dict[str, Any] = {
            "name": "test-job",
            "prompt": "Do something useful",
            "triggers": [{"type": "interval", "interval_seconds": 3600}],
        }
        resp = client.post("/jobs", json=payload, headers=auth)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-job"
        assert data["prompt"] == "Do something useful"
        assert "job_id" in data

    def test_create_job_persisted(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        client.post("/jobs", json={"name": "persist-me", "prompt": "test"}, headers=auth)
        jobs = load_jobs()
        assert any(j.name == "persist-me" for j in jobs)

    def test_create_job_no_auth_401(self, client: TestClient) -> None:
        assert client.post("/jobs", json={"name": "x", "prompt": "y"}).status_code == 401


# ---------------------------------------------------------------------------
# GET /jobs — list
# ---------------------------------------------------------------------------


class TestListJobs:
    def test_list_empty(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.get("/jobs", headers=auth)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_created_jobs(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        client.post("/jobs", json={"name": "job-a", "prompt": "a"}, headers=auth)
        client.post("/jobs", json={"name": "job-b", "prompt": "b"}, headers=auth)
        resp = client.get("/jobs", headers=auth)
        names = {j["name"] for j in resp.json()}
        assert {"job-a", "job-b"} <= names


# ---------------------------------------------------------------------------
# GET /jobs/{id}
# ---------------------------------------------------------------------------


class TestGetJob:
    def test_get_existing_job(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        created = client.post("/jobs", json={"name": "get-me", "prompt": "x"}, headers=auth).json()
        resp = client.get(f"/jobs/{created['job_id']}", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["name"] == "get-me"

    def test_get_nonexistent_404(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        assert client.get("/jobs/does-not-exist", headers=auth).status_code == 404


# ---------------------------------------------------------------------------
# DELETE /jobs/{id}
# ---------------------------------------------------------------------------


class TestDeleteJob:
    def test_delete_existing(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job_id = client.post("/jobs", json={"name": "delete-me", "prompt": "x"}, headers=auth).json()["job_id"]
        assert client.delete(f"/jobs/{job_id}", headers=auth).status_code == 204
        assert client.get(f"/jobs/{job_id}", headers=auth).status_code == 404

    def test_delete_nonexistent_404(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        assert client.delete("/jobs/no-such-job", headers=auth).status_code == 404


# ---------------------------------------------------------------------------
# POST /jobs/{id}/enable  &  /disable
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_disable_then_enable(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job_id = client.post("/jobs", json={"name": "toggle-me", "prompt": "x"}, headers=auth).json()["job_id"]
        resp = client.post(f"/jobs/{job_id}/disable", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        resp = client.post(f"/jobs/{job_id}/enable", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True


# ---------------------------------------------------------------------------
# POST /jobs/{id}/run — manual trigger
# ---------------------------------------------------------------------------


class TestTriggerJob:
    def test_trigger_dispatches_run(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="run-me", prompt="go")
        upsert_job(job)

        fake_run = JobRun(job_id=job.job_id, job_name=job.name, status=JobStatus.COMPLETED, output="done")

        async def _fake_run(j: AmbientJob, **kwargs: object) -> JobRun:
            return fake_run

        with patch("bog_agents_daemon.runner.run_job", side_effect=_fake_run):
            resp = client.post(f"/jobs/{job.job_id}/run", headers=auth)

        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "completed"
        assert data["output"] == "done"

    def test_trigger_nonexistent_404(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        assert client.post("/jobs/bad-id/run", headers=auth).status_code == 404


# ---------------------------------------------------------------------------
# GET /jobs/{id}/runs  &  GET /runs
# ---------------------------------------------------------------------------


class TestRunHistory:
    def test_runs_empty(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="no-runs", prompt="x")
        upsert_job(job)
        resp = client.get(f"/jobs/{job.job_id}/runs", headers=auth)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_all_runs_empty(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.get("/runs", headers=auth)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


class TestWebhooks:
    def test_git_push_triggers_matching_job(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="git-job", prompt="deploy")
        from bog_agents_daemon.models import TriggerConfig

        job.triggers = [TriggerConfig(type=TriggerType.GIT_PUSH, git_branch_pattern="main")]
        upsert_job(job)

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/git-push",
                json={"ref": "refs/heads/main", "new_sha": "abc", "old_sha": "def"},
                headers=auth,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert job.job_id in data["triggered"]

    def test_git_push_no_match_skips(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="other-branch", prompt="x")
        from bog_agents_daemon.models import TriggerConfig

        job.triggers = [TriggerConfig(type=TriggerType.GIT_PUSH, git_branch_pattern="develop")]
        upsert_job(job)

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/git-push",
                json={"ref": "refs/heads/main"},
                headers=auth,
            )

        assert resp.status_code == 200
        assert job.job_id not in resp.json()["triggered"]

    def test_webhook_triggers_matching_path(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = AmbientJob(name="webhook-job", prompt="build")
        from bog_agents_daemon.models import TriggerConfig

        job.triggers = [TriggerConfig(type=TriggerType.WEBHOOK, webhook_path="/hooks/ci")]
        upsert_job(job)

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post("/webhooks/hooks/ci", json={"event": "push"}, headers=auth)

        assert resp.status_code == 200
        assert job.job_id in resp.json()["triggered"]
