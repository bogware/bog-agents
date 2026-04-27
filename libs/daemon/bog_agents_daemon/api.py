"""FastAPI REST API for the bog-agents daemon."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

if TYPE_CHECKING:
    from bog_agents_daemon.models import JobRun
    from bog_agents_daemon.scheduler import DaemonScheduler
from pydantic import BaseModel

from bog_agents_daemon import __version__
from bog_agents_daemon.models import (
    AmbientJob,
    OutputConfig,
    OutputTarget,
    TriggerConfig,
    TriggerType,
)
from bog_agents_daemon.store import (
    delete_job,
    get_job,
    list_runs,
    load_jobs,
    upsert_job,
)

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 7391
_TOKEN_FILE = Path.home() / ".bog-agents" / "daemon" / "token"


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class TriggerConfigModel(BaseModel):
    """Pydantic schema for TriggerConfig."""

    type: str
    cron: str = ""
    interval_seconds: int = 0
    watch_patterns: list[str] = []
    watch_dir: str = ""
    debounce_seconds: float = 5.0
    webhook_path: str = ""
    webhook_secret: str = ""
    git_branch_pattern: str = "*"


class OutputConfigModel(BaseModel):
    """Pydantic schema for OutputConfig."""

    target: str
    file_path: str = ""
    append: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = []
    subject_template: str = "Bog Agents: {job_name} completed"
    slack_webhook_url: str = ""
    slack_channel: str = ""
    github_repo: str = ""
    github_issue_or_pr: int = 0
    github_token: str = ""
    webhook_url: str = ""
    webhook_headers: dict[str, str] = {}


class CreateJobRequest(BaseModel):
    """Request body for creating an ambient job."""

    name: str
    description: str = ""
    prompt: str = ""
    pipeline_name: str = ""
    skill_name: str = ""
    model: str = ""
    working_dir: str = ""
    triggers: list[TriggerConfigModel] = []
    outputs: list[OutputConfigModel] = []
    enabled: bool = True


class TriggerRunRequest(BaseModel):
    """Optional body for manual job trigger."""

    context: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _load_token() -> str | None:
    """Read the daemon auth token from disk.

    Returns:
        The token string, or None if the file does not exist.
    """
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    return None


def _check_auth(request: Request, token: str) -> None:
    """Verify the X-Daemon-Token header matches the expected token.

    Args:
        request: The incoming FastAPI request.
        token: The expected token value.

    Raises:
        HTTPException: With status 401 if the token is missing or wrong.
    """
    provided = request.headers.get("X-Daemon-Token", "")
    if not provided or not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _trigger_config_from_model(m: TriggerConfigModel) -> TriggerConfig:
    """Convert a Pydantic TriggerConfigModel to a dataclass TriggerConfig.

    Args:
        m: The Pydantic model instance.

    Returns:
        A TriggerConfig dataclass instance.
    """
    return TriggerConfig(
        type=TriggerType(m.type),
        cron=m.cron,
        interval_seconds=m.interval_seconds,
        watch_patterns=m.watch_patterns,
        watch_dir=m.watch_dir,
        debounce_seconds=m.debounce_seconds,
        webhook_path=m.webhook_path,
        webhook_secret=m.webhook_secret,
        git_branch_pattern=m.git_branch_pattern,
    )


def _output_config_from_model(m: OutputConfigModel) -> OutputConfig:
    """Convert a Pydantic OutputConfigModel to a dataclass OutputConfig.

    Args:
        m: The Pydantic model instance.

    Returns:
        An OutputConfig dataclass instance.
    """
    return OutputConfig(
        target=OutputTarget(m.target),
        file_path=m.file_path,
        append=m.append,
        smtp_host=m.smtp_host,
        smtp_port=m.smtp_port,
        smtp_username=m.smtp_username,
        smtp_password=m.smtp_password,
        from_addr=m.from_addr,
        to_addrs=m.to_addrs,
        subject_template=m.subject_template,
        slack_webhook_url=m.slack_webhook_url,
        slack_channel=m.slack_channel,
        github_repo=m.github_repo,
        github_issue_or_pr=m.github_issue_or_pr,
        github_token=m.github_token,
        webhook_url=m.webhook_url,
        webhook_headers=m.webhook_headers,
    )


def _job_to_response(job: AmbientJob) -> dict[str, Any]:
    """Serialize an AmbientJob to a JSON-safe dict for API responses.

    Args:
        job: The job to serialize.

    Returns:
        A dict suitable for returning from a FastAPI endpoint.
    """
    import dataclasses

    d = dataclasses.asdict(job)
    d["last_status"] = job.last_status.value
    for trigger in d.get("triggers", []):
        if "type" in trigger and hasattr(trigger["type"], "value"):
            trigger["type"] = trigger["type"].value
    for output in d.get("outputs", []):
        if "target" in output and hasattr(output["target"], "value"):
            output["target"] = output["target"].value
    return d


def _run_to_response(run: JobRun) -> dict[str, Any]:
    """Serialize a JobRun to a JSON-safe dict for API responses.

    Args:
        run: The JobRun to serialize.

    Returns:
        A dict suitable for returning from a FastAPI endpoint.
    """
    import dataclasses

    d = dataclasses.asdict(run)
    d["status"] = run.status.value
    d["trigger_type"] = run.trigger_type.value
    return d


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    token: str,
    scheduler: DaemonScheduler,
    request_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        token: The auth token used to authenticate API requests.
        scheduler: The DaemonScheduler instance (passed for future extension).
        request_shutdown: Optional callback the `/shutdown` endpoint invokes
            to ask the server to exit gracefully. When `None`, `/shutdown`
            returns 503.

    Returns:
        A configured FastAPI application.
    """
    app = FastAPI(
        title="Bog Agents Daemon",
        version=__version__,
        description="Ambient agent daemon REST API",
    )
    webhook_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Return daemon health and job count.

        Returns:
            Status dict with version and job count.
        """
        _check_auth(request, token)
        jobs = load_jobs()
        return {"status": "ok", "version": __version__, "job_count": len(jobs)}

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    @app.post("/shutdown", status_code=202)
    async def shutdown_endpoint(request: Request) -> dict[str, str]:
        """Request graceful shutdown of the daemon.

        Token-protected. Useful on Windows where signal-based termination
        through PID files is unreliable, and for scripted control.

        Returns:
            Status dict indicating shutdown was requested.

        Raises:
            HTTPException: 503 if no shutdown callback is wired.
        """
        _check_auth(request, token)
        if request_shutdown is None:
            raise HTTPException(status_code=503, detail="Shutdown callback not configured")
        request_shutdown()
        return {"status": "shutting down"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        """Kubernetes-style readiness probe — no auth required.

        Returns:
            Status dict indicating the service is ready.
        """
        return {"status": "ready"}

    # ------------------------------------------------------------------
    # Jobs collection
    # ------------------------------------------------------------------

    @app.get("/jobs")
    async def list_jobs_endpoint(request: Request) -> list[dict[str, Any]]:
        """List all configured jobs.

        Returns:
            List of job dicts.
        """
        _check_auth(request, token)
        return [_job_to_response(j) for j in load_jobs()]

    @app.post("/jobs", status_code=201)
    async def create_job_endpoint(request: Request, body: CreateJobRequest) -> dict[str, Any]:
        """Create a new ambient job.

        Args:
            body: The job creation parameters.

        Returns:
            The newly created job dict.
        """
        _check_auth(request, token)
        job = AmbientJob(
            name=body.name,
            description=body.description,
            prompt=body.prompt,
            pipeline_name=body.pipeline_name,
            skill_name=body.skill_name,
            model=body.model,
            working_dir=body.working_dir,
            triggers=[_trigger_config_from_model(t) for t in body.triggers],
            outputs=[_output_config_from_model(o) for o in body.outputs],
            enabled=body.enabled,
        )
        upsert_job(job)
        logger.info("Created job %s (%s)", job.job_id, job.name)
        return _job_to_response(job)

    # ------------------------------------------------------------------
    # Single job
    # ------------------------------------------------------------------

    @app.get("/jobs/{job_id}")
    async def get_job_endpoint(request: Request, job_id: str) -> dict[str, Any]:
        """Get a single job by ID.

        Args:
            job_id: The job identifier.

        Returns:
            The job dict.

        Raises:
            HTTPException: 404 if not found.
        """
        _check_auth(request, token)
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return _job_to_response(job)

    @app.delete("/jobs/{job_id}", status_code=204)
    async def delete_job_endpoint(request: Request, job_id: str) -> None:
        """Delete a job by ID.

        Args:
            job_id: The job identifier.

        Raises:
            HTTPException: 404 if not found.
        """
        _check_auth(request, token)
        if not delete_job(job_id):
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        logger.info("Deleted job %s", job_id)

    @app.post("/jobs/{job_id}/run", status_code=202)
    async def trigger_job_endpoint(request: Request, job_id: str, body: TriggerRunRequest | None = None) -> dict[str, Any]:
        """Trigger a manual run of a job.

        The run is started in the background and the initial JobRun record
        (status=running) is returned immediately so that HTTP clients with
        short timeouts don't disconnect during a long-running agent
        invocation. Poll `/jobs/{job_id}/runs` for completion state.

        Args:
            job_id: The job identifier.
            body: Optional trigger context.

        Returns:
            A JobRun dict with status=running and a freshly assigned run_id.

        Raises:
            HTTPException: 404 if not found.
        """
        _check_auth(request, token)
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        import asyncio

        from bog_agents_daemon.models import JobRun, JobStatus
        from bog_agents_daemon.runner import run_job
        from bog_agents_daemon.store import save_run

        context = body.context if body else {}

        # Persist a placeholder run record up front so callers (and the CLI's
        # `jobs run`) see a real run_id and status=running immediately.
        run = JobRun(
            job_id=job.job_id,
            job_name=job.name,
            status=JobStatus.RUNNING,
            trigger_type=TriggerType.MANUAL,
            trigger_context=context,
        )
        save_run(run)

        async def _do_run() -> None:
            with contextlib.suppress(Exception):
                # Background task — run_job already logs internally.
                await run_job(
                    job,
                    trigger_type=TriggerType.MANUAL,
                    trigger_context=context,
                    _existing_run=run,
                )

        # Hold a strong reference so the loop doesn't garbage-collect
        # the task mid-flight (RUF006); discarded from the set on done.
        bg_task = asyncio.create_task(_do_run())
        webhook_tasks.add(bg_task)
        bg_task.add_done_callback(webhook_tasks.discard)
        return _run_to_response(run)

    # ------------------------------------------------------------------
    # Job runs
    # ------------------------------------------------------------------

    @app.get("/jobs/{job_id}/runs")
    async def list_job_runs_endpoint(request: Request, job_id: str) -> list[dict[str, Any]]:
        """List run history for a specific job.

        Args:
            job_id: The job identifier.

        Returns:
            List of run dicts sorted newest-first.
        """
        _check_auth(request, token)
        runs = list_runs(job_id=job_id)
        return [_run_to_response(r) for r in runs]

    @app.get("/runs")
    async def list_all_runs_endpoint(request: Request) -> list[dict[str, Any]]:
        """List recent runs across all jobs.

        Returns:
            List of up to 20 run dicts sorted newest-first.
        """
        _check_auth(request, token)
        runs = list_runs(limit=20)
        return [_run_to_response(r) for r in runs]

    # ------------------------------------------------------------------
    # Enable / Disable
    # ------------------------------------------------------------------

    @app.post("/jobs/{job_id}/enable")
    async def enable_job_endpoint(request: Request, job_id: str) -> dict[str, Any]:
        """Enable a disabled job.

        Args:
            job_id: The job identifier.

        Returns:
            Updated job dict.

        Raises:
            HTTPException: 404 if not found.
        """
        _check_auth(request, token)
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        job.enabled = True
        upsert_job(job)
        return _job_to_response(job)

    @app.post("/jobs/{job_id}/disable")
    async def disable_job_endpoint(request: Request, job_id: str) -> dict[str, Any]:
        """Disable an enabled job without deleting it.

        Args:
            job_id: The job identifier.

        Returns:
            Updated job dict.

        Raises:
            HTTPException: 404 if not found.
        """
        _check_auth(request, token)
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        job.enabled = False
        upsert_job(job)
        return _job_to_response(job)

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    @app.post("/webhooks/git-push")
    async def receive_git_push(request: Request) -> dict[str, Any]:
        """Handle a git post-receive push event from the installed git hook.

        Triggers any enabled jobs that have a GIT_PUSH trigger whose
        `git_branch_pattern` matches the pushed refname.  The hook sends JSON
        with `ref`, `new_sha`, and `old_sha` fields.

        Returns:
            Dict with triggered job IDs and count.
        """
        import fnmatch as _fnmatch

        _check_auth(request, token)
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        ref = payload.get("ref", "")
        # Normalise "refs/heads/main" → "main"
        branch = ref.split("/")[-1] if ref else ""

        jobs = load_jobs()
        triggered: list[str] = []
        for job in jobs:
            if not job.enabled:
                continue
            for trigger in job.triggers:
                if trigger.type != TriggerType.GIT_PUSH:
                    continue
                pattern = trigger.git_branch_pattern or "*"
                if not _fnmatch.fnmatch(branch, pattern):
                    continue
                from bog_agents_daemon.runner import run_job

                task = asyncio.create_task(
                    run_job(
                        job,
                        trigger_type=TriggerType.GIT_PUSH,
                        trigger_context={"ref": ref, "branch": branch, **payload},
                    )
                )
                webhook_tasks.add(task)
                task.add_done_callback(webhook_tasks.discard)
                triggered.append(job.job_id)
                break

        return {"triggered": triggered, "count": len(triggered)}

    @app.post("/webhooks/{webhook_path:path}")
    async def receive_webhook(request: Request, webhook_path: str) -> dict[str, Any]:
        """Receive an inbound webhook and trigger any matching jobs.

        Matches jobs whose triggers include a WEBHOOK trigger with a
        `webhook_path` ending in the provided path segment.  When the trigger
        has a `webhook_secret`, the request body must carry a valid
        `X-Hub-Signature-256` header (HMAC-SHA256 of the raw body).

        Args:
            webhook_path: The URL path suffix after /webhooks/.

        Returns:
            Dict reporting how many jobs were triggered.
        """
        _check_auth(request, token)
        raw_body = await request.body()
        try:
            import json as _json

            payload = _json.loads(raw_body) if raw_body else {}
        except Exception:
            payload = {}

        normalized = webhook_path.lstrip("/")
        jobs = load_jobs()
        triggered: list[str] = []

        for job in jobs:
            if not job.enabled:
                continue
            for trigger in job.triggers:
                if trigger.type != TriggerType.WEBHOOK:
                    continue
                trigger_path = trigger.webhook_path.lstrip("/")
                if trigger_path != normalized:
                    continue
                # HMAC secret validation — reject if secret configured but sig absent/wrong
                if trigger.webhook_secret:
                    sig_header = request.headers.get("X-Hub-Signature-256", "")
                    expected = (
                        "sha256="
                        + hmac.new(
                            trigger.webhook_secret.encode(),
                            raw_body,
                            hashlib.sha256,
                        ).hexdigest()
                    )
                    if not hmac.compare_digest(sig_header, expected):
                        logger.warning(
                            "Webhook signature mismatch for job %s trigger path %s",
                            job.job_id,
                            webhook_path,
                        )
                        break  # wrong secret — don't trigger this job
                from bog_agents_daemon.runner import run_job

                task = asyncio.create_task(
                    run_job(
                        job,
                        trigger_type=TriggerType.WEBHOOK,
                        trigger_context={"path": webhook_path, "payload": payload},
                    )
                )
                webhook_tasks.add(task)
                task.add_done_callback(webhook_tasks.discard)
                triggered.append(job.job_id)
                break

        return {"triggered": triggered, "count": len(triggered)}

    return app
