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

    def test_webhook_honors_rotated_token(
        self,
        client: TestClient,
        auth: dict,
        tmp_daemon_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # DMN-2: after /admin/rotate-token the webhook path must reject the old
        # token and accept the new one. It previously compared against a stale
        # closure, so a leaked old token authenticated here forever.
        import bog_agents_daemon.api as api_mod
        from bog_agents_daemon.models import TriggerConfig

        # Keep the rotated token write inside tmp_path, not the real home dir.
        monkeypatch.setattr(api_mod, "_TOKEN_FILE", tmp_path / "token")

        job = AmbientJob(name="rotate-hook", prompt="build")
        job.triggers = [TriggerConfig(type=TriggerType.WEBHOOK, webhook_path="/hooks/ci")]
        upsert_job(job)

        new_token = client.post("/admin/rotate-token", headers=auth).json()["token"]
        assert new_token != _TEST_TOKEN

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            # Old token: no longer valid and the trigger has no HMAC secret → skipped.
            old = client.post("/webhooks/hooks/ci", json={"event": "push"}, headers={"X-Daemon-Token": _TEST_TOKEN})
            assert old.status_code == 200
            assert job.job_id not in old.json()["triggered"]

            # New token: authenticates → triggered.
            new = client.post("/webhooks/hooks/ci", json={"event": "push"}, headers={"X-Daemon-Token": new_token})
            assert new.status_code == 200
            assert job.job_id in new.json()["triggered"]


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


# ---------------------------------------------------------------------------
# GitHub webhook (#30, Assign-to-bog)
# ---------------------------------------------------------------------------


class TestGitHubWebhook:
    def test_issue_assigned_triggers_github_job(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        from bog_agents_daemon.models import TriggerConfig

        job = AmbientJob(name="gh", prompt="fix the issue")
        job.triggers = [TriggerConfig(type=TriggerType.GITHUB)]
        upsert_job(job)

        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/github",
                json={"action": "assigned", "assignee": {"login": "bot"}, "issue": {"number": 5, "title": "t", "body": "b"}},
                headers={**auth, "X-GitHub-Event": "issues"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert job.job_id in body["triggered"]
        assert body["kind"] == "issue_assigned"

    def test_non_actionable_event_triggers_nothing(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        from bog_agents_daemon.models import TriggerConfig

        job = AmbientJob(name="gh", prompt="x")
        job.triggers = [TriggerConfig(type=TriggerType.GITHUB)]
        upsert_job(job)

        resp = client.post(
            "/webhooks/github",
            json={"action": "opened", "issue": {"number": 1}},
            headers={**auth, "X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["triggered"] == []
        assert resp.json()["actionable"] is False

    def test_unsigned_request_without_token_refused(self, client: TestClient, tmp_daemon_dir: Path) -> None:
        # No daemon token AND no configured GitHub secret → fail closed.
        from bog_agents_daemon.models import TriggerConfig

        job = AmbientJob(name="gh", prompt="x")
        job.triggers = [TriggerConfig(type=TriggerType.GITHUB)]
        upsert_job(job)

        resp = client.post(
            "/webhooks/github",
            json={"action": "assigned", "assignee": {"login": "bot"}, "issue": {"number": 5}},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["triggered"] == []


# ---------------------------------------------------------------------------
# Git-push branch filter — full-name matching (DMN-7)
# ---------------------------------------------------------------------------


class TestGitPushBranchFilter:
    """DMN-7: `git_branch_pattern` matches the FULL branch name.

    Only the `refs/heads/` (or `refs/tags/`) prefix is stripped from the
    pushed ref, so slashed branch names survive intact and `feature/*`
    style patterns work. The old last-segment split (`ref.split("/")[-1]`)
    reduced `refs/heads/feature/login` to `login`, which made every
    slash-containing pattern unmatchable by any push.
    """

    def _make_git_job(self, pattern: str) -> AmbientJob:
        from bog_agents_daemon.models import TriggerConfig

        job = AmbientJob(name=f"git-{pattern}", prompt="deploy")
        job.triggers = [TriggerConfig(type=TriggerType.GIT_PUSH, git_branch_pattern=pattern)]
        upsert_job(job)
        return job

    def _push(self, client: TestClient, auth: dict, ref: str) -> list[str]:
        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            resp = client.post(
                "/webhooks/git-push",
                json={"ref": ref, "new_sha": "abc", "old_sha": "def"},
                headers=auth,
            )
        assert resp.status_code == 200
        return resp.json()["triggered"]

    def test_slashed_pattern_matches_slashed_branch(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = self._make_git_job("feature/*")
        assert job.job_id in self._push(client, auth, "refs/heads/feature/login")

    def test_release_pattern_matches_versioned_branch(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = self._make_git_job("release/*")
        assert job.job_id in self._push(client, auth, "refs/heads/release/1.0")

    def test_bare_name_still_matches(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = self._make_git_job("main")
        assert job.job_id in self._push(client, auth, "refs/heads/main")

    def test_last_segment_pattern_does_not_match_slashed_branch(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        # Full-name matching semantics: `login` is not `feature/login`.
        job = self._make_git_job("login")
        assert job.job_id not in self._push(client, auth, "refs/heads/feature/login")

    def test_bare_pattern_does_not_match_prefixed_branch(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        # Regression guard for the old over-match: refs/heads/wip/main used to
        # collapse to `main` and fire a `main`-patterned job.
        job = self._make_git_job("main")
        assert job.job_id not in self._push(client, auth, "refs/heads/wip/main")

    def test_tag_ref_matches_against_tag_name(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = self._make_git_job("v*")
        assert job.job_id in self._push(client, auth, "refs/tags/v1.0")

    def test_unprefixed_ref_matches_as_is(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        # A payload carrying a bare branch name (no refs/heads/ prefix) still works.
        job = self._make_git_job("main")
        assert job.job_id in self._push(client, auth, "main")

    def test_wildcard_still_matches_slashed_branch(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        job = self._make_git_job("*")
        assert job.job_id in self._push(client, auth, "refs/heads/feature/login")


# ---------------------------------------------------------------------------
# Secret redaction round-trip (DMN-8)
# ---------------------------------------------------------------------------


class TestSecretRedactionRoundTrip:
    """DMN-8: the `'***'` redaction placeholder is never stored as a real secret.

    GET/list redact webhook_secret / smtp_password / github_token to `'***'`;
    the natural GET-edit-PATCH round-trip used to write that literal string
    back as the secret — making the webhook HMAC forgeable by anyone and
    silently breaking email/GitHub delivery auth.
    """

    _SECRET = "real-hmac-secret"

    def _create_full_job(self, client: TestClient, auth: dict) -> dict:
        payload = {
            "name": "secret-job",
            "prompt": "x",
            "triggers": [
                {"type": "webhook", "webhook_path": "/hooks/ci", "webhook_secret": self._SECRET},
            ],
            "outputs": [
                {"target": "email", "smtp_host": "mail.example.com", "smtp_password": "smtp-pass", "to_addrs": ["ops@example.com"]},
                {"target": "github_comment", "github_repo": "owner/repo", "github_issue_or_pr": 1, "github_token": "ghp_real"},
            ],
        }
        resp = client.post("/jobs", json=payload, headers=auth)
        assert resp.status_code == 201
        return resp.json()

    def test_get_redacts_all_secret_fields(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        created = self._create_full_job(client, auth)
        fetched = client.get(f"/jobs/{created['job_id']}", headers=auth).json()
        assert fetched["triggers"][0]["webhook_secret"] == "***"
        assert fetched["outputs"][0]["smtp_password"] == "***"
        assert fetched["outputs"][1]["github_token"] == "***"

    def test_patch_round_trip_preserves_stored_secrets(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """The natural GET → edit → PATCH flow keeps the real stored secrets."""
        created = self._create_full_job(client, auth)
        job_id = created["job_id"]
        fetched = client.get(f"/jobs/{job_id}", headers=auth).json()

        # Simulate a client editing one unrelated field and sending the whole
        # (redacted) triggers/outputs arrays back.
        triggers = fetched["triggers"]
        outputs = fetched["outputs"]
        outputs[0]["smtp_host"] = "mail2.example.com"
        resp = client.patch(f"/jobs/{job_id}", json={"triggers": triggers, "outputs": outputs}, headers=auth)
        assert resp.status_code == 200

        stored = next(j for j in load_jobs() if j.job_id == job_id)
        assert stored.triggers[0].webhook_secret == self._SECRET
        assert stored.outputs[0].smtp_password == "smtp-pass"
        assert stored.outputs[0].smtp_host == "mail2.example.com"  # the actual edit landed
        assert stored.outputs[1].github_token == "ghp_real"

    def test_patch_with_new_real_value_replaces_secret(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        created = self._create_full_job(client, auth)
        job_id = created["job_id"]
        resp = client.patch(
            f"/jobs/{job_id}",
            json={"triggers": [{"type": "webhook", "webhook_path": "/hooks/ci", "webhook_secret": "rotated-secret"}]},
            headers=auth,
        )
        assert resp.status_code == 200
        stored = next(j for j in load_jobs() if j.job_id == job_id)
        assert stored.triggers[0].webhook_secret == "rotated-secret"

    def test_patched_placeholder_secret_still_authenticates_hmac(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        """After a redacted round-trip, the ORIGINAL secret still signs webhooks
        and the literal '***' does not — the security consequence of DMN-8."""
        import hashlib
        import hmac
        import json

        created = self._create_full_job(client, auth)
        job_id = created["job_id"]
        fetched = client.get(f"/jobs/{job_id}", headers=auth).json()
        resp = client.patch(f"/jobs/{job_id}", json={"triggers": fetched["triggers"]}, headers=auth)
        assert resp.status_code == 200

        payload = json.dumps({"event": "x"}).encode("utf-8")
        with patch("bog_agents_daemon.runner.run_job", new_callable=AsyncMock):
            good_sig = "sha256=" + hmac.new(self._SECRET.encode(), payload, hashlib.sha256).hexdigest()
            good = client.post(
                "/webhooks/hooks/ci",
                content=payload,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": good_sig},
            )
            assert job_id in good.json()["triggered"]

            forged_sig = "sha256=" + hmac.new(b"***", payload, hashlib.sha256).hexdigest()
            forged = client.post(
                "/webhooks/hooks/ci",
                content=payload,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": forged_sig},
            )
            assert job_id not in forged.json()["triggered"]

    def test_create_with_placeholder_secret_is_rejected_422(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.post(
            "/jobs",
            json={
                "name": "bad",
                "prompt": "x",
                "triggers": [{"type": "webhook", "webhook_path": "/hooks/x", "webhook_secret": "***"}],
            },
            headers=auth,
        )
        assert resp.status_code == 422
        assert "placeholder" in resp.json()["detail"]
        assert load_jobs() == []

    def test_create_with_placeholder_output_secret_is_rejected_422(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        resp = client.post(
            "/jobs",
            json={
                "name": "bad",
                "prompt": "x",
                "outputs": [{"target": "email", "smtp_password": "***", "to_addrs": ["x@y.z"]}],
            },
            headers=auth,
        )
        assert resp.status_code == 422
        assert load_jobs() == []

    def test_patch_placeholder_with_nothing_to_preserve_is_rejected_422(self, client: TestClient, auth: dict, tmp_daemon_dir: Path) -> None:
        # Adding a NEW trigger with '***' (no stored counterpart at that
        # position) must fail loudly rather than store the placeholder.
        created = self._create_full_job(client, auth)
        job_id = created["job_id"]
        fetched = client.get(f"/jobs/{job_id}", headers=auth).json()
        new_triggers = [
            *fetched["triggers"],
            {"type": "webhook", "webhook_path": "/hooks/other", "webhook_secret": "***"},
        ]
        resp = client.patch(f"/jobs/{job_id}", json={"triggers": new_triggers}, headers=auth)
        assert resp.status_code == 422
        # The stored job is untouched.
        stored = next(j for j in load_jobs() if j.job_id == job_id)
        assert len(stored.triggers) == 1
        assert stored.triggers[0].webhook_secret == self._SECRET
