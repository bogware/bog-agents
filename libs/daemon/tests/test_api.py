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

    def test_create_with_retry_policy(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.post(
            "/jobs",
            json={"name": "retry-job", "prompt": "x", "max_retries": 3, "retry_backoff_seconds": 1.5},
            headers=auth,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["max_retries"] == 3
        assert data["retry_backoff_seconds"] == 1.5

    def test_retry_policy_defaults_when_omitted(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        data = client.post("/jobs", json={"name": "default-retry", "prompt": "x"}, headers=auth).json()
        assert data["max_retries"] == 0
        assert data["retry_backoff_seconds"] == 2.0

    def test_max_retries_over_cap_rejected(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.post("/jobs", json={"name": "greedy", "prompt": "x", "max_retries": 999}, headers=auth)
        assert resp.status_code == 422


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
# Request-body validation — extra fields + invalid enum values
# ---------------------------------------------------------------------------


class TestRequestValidation:
    """Workday regression tests for daemon request-body validation.

    Background:

    Before these guards, a caller could POST ``{"trigger": {...}, "output": {...}}``
    (singular, looks like English) and the daemon would silently drop the
    unknown fields, store a job with empty triggers/outputs, and accept it
    with HTTP 201 — so the job never fired and the user had no clue why.
    Invalid enum values like ``triggers[0].type = "nonsense"`` returned
    HTTP 500 (server crash on ``TriggerType(v)`` ValueError) instead of
    a clean 422.
    """

    def test_singular_trigger_typo_returns_422(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """The schema uses ``triggers`` (plural). A singular ``trigger`` is rejected."""
        resp = client.post(
            "/jobs",
            json={"name": "x", "prompt": "y", "trigger": {"type": "cron"}},
            headers=auth,
        )
        assert resp.status_code == 422
        body = resp.json()
        assert any(err["type"] == "extra_forbidden" for err in body["detail"])

    def test_singular_output_typo_returns_422(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """Same rule for ``output`` (singular) vs ``outputs`` (plural)."""
        resp = client.post(
            "/jobs",
            json={"name": "x", "prompt": "y", "output": {"target": "file"}},
            headers=auth,
        )
        assert resp.status_code == 422

    def test_unknown_top_level_field_returns_422(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """Misspelled top-level fields are surfaced, not silently dropped."""
        resp = client.post(
            "/jobs",
            json={"name": "x", "prompt": "y", "trigggers": []},
            headers=auth,
        )
        assert resp.status_code == 422

    def test_invalid_trigger_type_returns_422_not_500(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """A bogus trigger type 422s with a precise message — not 500."""
        resp = client.post(
            "/jobs",
            json={"name": "x", "prompt": "y", "triggers": [{"type": "nonsense"}]},
            headers=auth,
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert any("nonsense" in err.get("msg", "") for err in detail)
        # The error message lists every valid value so the user can pick.
        msg = " ".join(err.get("msg", "") for err in detail)
        for valid in ("cron", "file_change", "webhook", "manual", "interval"):
            assert valid in msg

    def test_invalid_output_target_returns_422_not_500(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """Bogus output target same treatment."""
        resp = client.post(
            "/jobs",
            json={"name": "x", "prompt": "y", "outputs": [{"target": "telegram"}]},
            headers=auth,
        )
        assert resp.status_code == 422
        msg = " ".join(err.get("msg", "") for err in resp.json()["detail"])
        assert "telegram" in msg
        for valid in ("file", "email", "slack", "stdout"):
            assert valid in msg

    def test_correctly_shaped_request_still_201(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """The strict validation must not break correctly-shaped requests."""
        resp = client.post(
            "/jobs",
            json={
                "name": "valid",
                "prompt": "y",
                "triggers": [{"type": "cron", "cron": "0 9 * * *"}],
                "outputs": [{"target": "file", "file_path": "/tmp/x"}],
            },
            headers=auth,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["triggers"]) == 1
        assert body["triggers"][0]["type"] == "cron"
        assert len(body["outputs"]) == 1
        assert body["outputs"][0]["target"] == "file"


# ---------------------------------------------------------------------------
# PATCH /jobs/{id} — partial edit
# ---------------------------------------------------------------------------


class TestPatchJob:
    def test_patch_prompt_persists(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job_id = client.post("/jobs", json={"name": "edit-me", "prompt": "before"}, headers=auth).json()["job_id"]
        resp = client.patch(f"/jobs/{job_id}", json={"prompt": "after"}, headers=auth)
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "after"
        # GET should reflect the patched value too.
        assert client.get(f"/jobs/{job_id}", headers=auth).json()["prompt"] == "after"

    def test_patch_preserves_unset_fields(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job_id = client.post(
            "/jobs",
            json={"name": "keep-me", "prompt": "original", "description": "OG"},
            headers=auth,
        ).json()["job_id"]
        resp = client.patch(f"/jobs/{job_id}", json={"prompt": "new"}, headers=auth)
        body = resp.json()
        assert body["prompt"] == "new"
        # description was not in payload — must remain unchanged.
        assert body["description"] == "OG"

    def test_patch_with_empty_body_is_noop_and_returns_existing(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job_id = client.post("/jobs", json={"name": "noop-me", "prompt": "x"}, headers=auth).json()["job_id"]
        resp = client.patch(f"/jobs/{job_id}", json={}, headers=auth)
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "x"

    def test_patch_unknown_returns_404(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.patch("/jobs/no-such-job", json={"prompt": "anything"}, headers=auth)
        assert resp.status_code == 404

    def test_patch_no_auth_returns_401(self, client: TestClient) -> None:
        resp = client.patch("/jobs/whatever", json={"prompt": "x"})
        assert resp.status_code == 401

    def test_patch_can_toggle_enabled(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job_id = client.post(
            "/jobs",
            json={"name": "toggle-me", "prompt": "x", "enabled": True},
            headers=auth,
        ).json()["job_id"]
        resp = client.patch(f"/jobs/{job_id}", json={"enabled": False}, headers=auth)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


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
        # The endpoint now returns immediately with status=running so HTTP
        # clients with short timeouts don't disconnect during long-running
        # agent invocations; the actual run happens in the background.
        job = AmbientJob(name="run-me", prompt="go")
        upsert_job(job)

        async def _fake_run(j: AmbientJob, **kwargs: object) -> JobRun:
            existing = kwargs.get("_existing_run")
            if existing is not None:
                existing.status = JobStatus.COMPLETED
                existing.output = "done"
                return existing
            return JobRun(job_id=j.job_id, job_name=j.name, status=JobStatus.COMPLETED, output="done")

        with patch("bog_agents_daemon.runner.run_job", side_effect=_fake_run):
            resp = client.post(f"/jobs/{job.job_id}/run", headers=auth)

        assert resp.status_code == 202
        data = resp.json()
        # The response is the placeholder run record — status=running, no
        # output yet. Callers poll /jobs/{id}/runs for the terminal state.
        assert data["status"] == "running"
        assert data["job_id"] == job.job_id
        assert data["run_id"]

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


# ---------------------------------------------------------------------------
# Webhook auth — fail-closed security contract
# ---------------------------------------------------------------------------


class TestWebhookFailClosed:
    """Pin the safe-by-default behaviour the audit (PRINCIPAL_REVIEW §2.3) checks.

    Historically the header docstring above the webhook handler said
    "empty secret = public entry point" — that was aspirational text
    that never matched the rejection logic in the handler. These
    tests pin the actual fail-closed behaviour so neither the comment
    NOR the code can drift back to admitting unauthenticated
    callers when ``webhook_secret`` is empty.
    """

    def _make_webhook_job(
        self,
        *,
        secret: str | None,
        path: str = "/hooks/external",
    ) -> AmbientJob:
        from bog_agents_daemon.models import TriggerConfig

        job = AmbientJob(name="external-hook", prompt="ack")
        job.triggers = [
            TriggerConfig(
                type=TriggerType.WEBHOOK,
                webhook_path=path,
                webhook_secret=secret,
            )
        ]
        upsert_job(job)
        return job

    def test_empty_secret_rejects_unauthenticated_request(self, client: TestClient, tmp_daemon_dir: Path) -> None:
        """Empty secret + no token → trigger MUST NOT fire."""
        job = self._make_webhook_job(secret="")

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            # No X-Daemon-Token header, no X-Hub-Signature-256 header.
            resp = client.post("/webhooks/hooks/external", json={"event": "x"})

        assert resp.status_code == 200, "endpoint always returns 200 for valid path"
        # The triggered list MUST NOT include this job — empty secret
        # means misconfigured, not public.
        assert job.job_id not in resp.json()["triggered"], (
            "Empty webhook_secret without an authenticated daemon token "
            "MUST NOT fire the job. See PRINCIPAL_REVIEW.md §2.3 and the "
            "fail-closed comment block above receive_webhook()."
        )

    def test_none_secret_rejects_unauthenticated_request(self, client: TestClient, tmp_daemon_dir: Path) -> None:
        """`secret=None` is treated identically to empty secret — also rejected."""
        job = self._make_webhook_job(secret=None)

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post("/webhooks/hooks/external", json={"event": "x"})

        assert resp.status_code == 200
        assert job.job_id not in resp.json()["triggered"]

    def test_empty_secret_still_rejects_with_wrong_token(self, client: TestClient, tmp_daemon_dir: Path) -> None:
        """Bogus X-Daemon-Token must not unlock an empty-secret trigger."""
        job = self._make_webhook_job(secret="")

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/hooks/external",
                json={"event": "x"},
                headers={"X-Daemon-Token": "wrong-token"},
            )

        assert resp.status_code == 200
        assert job.job_id not in resp.json()["triggered"]

    def test_valid_token_bypasses_empty_secret_rejection(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """The CLI test path: valid daemon token DOES fire even with empty secret.

        This is intentional — the local CLI test harness needs to fire
        webhooks against its own daemon without configuring HMAC.
        """
        job = self._make_webhook_job(secret="")

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/hooks/external",
                json={"event": "x"},
                headers=auth,
            )

        assert resp.status_code == 200
        assert job.job_id in resp.json()["triggered"]

    def test_valid_signature_fires_without_token(self, client: TestClient, tmp_daemon_dir: Path) -> None:
        """Configured secret + matching HMAC → trigger fires (no daemon token)."""
        import hashlib
        import hmac
        import json

        secret = "shared-deploy-secret"
        job = self._make_webhook_job(secret=secret)
        payload = json.dumps({"event": "x"}).encode("utf-8")
        signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/hooks/external",
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )

        assert resp.status_code == 200
        assert job.job_id in resp.json()["triggered"]

    def test_mismatched_signature_does_not_fire(self, client: TestClient, tmp_daemon_dir: Path) -> None:
        """Configured secret + wrong HMAC → trigger MUST NOT fire."""
        job = self._make_webhook_job(secret="shared-deploy-secret")

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/hooks/external",
                json={"event": "x"},
                headers={"X-Hub-Signature-256": "sha256=deadbeef"},
            )

        assert resp.status_code == 200
        assert job.job_id not in resp.json()["triggered"]
