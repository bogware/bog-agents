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
from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    model_config = ConfigDict(extra="forbid")

    type: str

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        valid = {t.value for t in TriggerType}
        if v not in valid:
            msg = f"invalid trigger type {v!r} — must be one of {sorted(valid)}"
            raise ValueError(msg)
        return v

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

    model_config = ConfigDict(extra="forbid")

    target: str

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str) -> str:
        valid = {t.value for t in OutputTarget}
        if v not in valid:
            msg = f"invalid output target {v!r} — must be one of {sorted(valid)}"
            raise ValueError(msg)
        return v

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


# Size caps. Generous enough for legitimate use, tight enough that a single
# request cannot blow up daemon memory.
_MAX_NAME_LEN = 256
_MAX_DESCRIPTION_LEN = 4_000
_MAX_PROMPT_LEN = 200_000
_MAX_PIPELINE_NAME_LEN = 256
_MAX_SKILL_NAME_LEN = 256
_MAX_MODEL_LEN = 256
_MAX_WORKING_DIR_LEN = 4_096
_MAX_TRIGGERS = 64
_MAX_OUTPUTS = 64
# Retry policy bounds — capped so a job can't spin unbounded or wait forever.
_MAX_RETRIES = 10
_MAX_RETRY_BACKOFF_SECONDS = 3_600.0


class CreateJobRequest(BaseModel):
    """Request body for creating an ambient job."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., max_length=_MAX_NAME_LEN, min_length=1)
    description: str = Field("", max_length=_MAX_DESCRIPTION_LEN)
    prompt: str = Field("", max_length=_MAX_PROMPT_LEN)
    pipeline_name: str = Field("", max_length=_MAX_PIPELINE_NAME_LEN)
    skill_name: str = Field("", max_length=_MAX_SKILL_NAME_LEN)
    model: str = Field("", max_length=_MAX_MODEL_LEN)
    working_dir: str = Field("", max_length=_MAX_WORKING_DIR_LEN)
    max_retries: int = Field(0, ge=0, le=_MAX_RETRIES)
    retry_backoff_seconds: float = Field(2.0, ge=0.0, le=_MAX_RETRY_BACKOFF_SECONDS)
    triggers: list[TriggerConfigModel] = Field(default_factory=list, max_length=_MAX_TRIGGERS)
    outputs: list[OutputConfigModel] = Field(default_factory=list, max_length=_MAX_OUTPUTS)
    enabled: bool = True


class TriggerRunRequest(BaseModel):
    """Optional body for manual job trigger."""

    model_config = ConfigDict(extra="forbid")

    context: dict[str, Any] = {}


class UpdateJobRequest(BaseModel):
    """Partial update for an existing ambient job.

    Every field is optional. Only the fields the caller sends are
    overwritten on the stored record; everything else is preserved.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=_MAX_NAME_LEN)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)
    prompt: str | None = Field(default=None, max_length=_MAX_PROMPT_LEN)
    pipeline_name: str | None = Field(default=None, max_length=_MAX_PIPELINE_NAME_LEN)
    skill_name: str | None = Field(default=None, max_length=_MAX_SKILL_NAME_LEN)
    model: str | None = Field(default=None, max_length=_MAX_MODEL_LEN)
    working_dir: str | None = Field(default=None, max_length=_MAX_WORKING_DIR_LEN)
    max_retries: int | None = Field(default=None, ge=0, le=_MAX_RETRIES)
    retry_backoff_seconds: float | None = Field(default=None, ge=0.0, le=_MAX_RETRY_BACKOFF_SECONDS)
    triggers: list[TriggerConfigModel] | None = Field(default=None, max_length=_MAX_TRIGGERS)
    outputs: list[OutputConfigModel] | None = Field(default=None, max_length=_MAX_OUTPUTS)
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _load_token() -> str | None:
    """Read the daemon auth token from disk.

    Returns:
        The token string, or None if the file does not exist.
    """
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
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


_REDACTED_OUTPUT_FIELDS: tuple[str, ...] = (
    "smtp_password",
    "github_token",
    "webhook_secret",
)
_REDACTED_TRIGGER_FIELDS: tuple[str, ...] = ("webhook_secret",)
_REDACTED_PLACEHOLDER = "***"


def _job_to_response(job: AmbientJob) -> dict[str, Any]:
    """Serialize an AmbientJob to a JSON-safe dict for API responses.

    Secrets that the daemon needs at run time (SMTP password, GitHub
    token, webhook HMAC secret) are persisted on disk in jobs.json but
    must not be echoed back through the HTTP API — anyone with valid
    daemon-token access could otherwise read them. We redact them to a
    fixed placeholder so the field shape stays stable for clients but
    the actual value is never exposed.

    Args:
        job: The job to serialize.

    Returns:
        A dict suitable for returning from a FastAPI endpoint, with
        secret-bearing fields replaced by `'***'` when present.
    """
    import dataclasses

    d = dataclasses.asdict(job)
    d["last_status"] = job.last_status.value
    for trigger in d.get("triggers", []):
        if "type" in trigger and hasattr(trigger["type"], "value"):
            trigger["type"] = trigger["type"].value
        for redacted in _REDACTED_TRIGGER_FIELDS:
            if trigger.get(redacted):
                trigger[redacted] = _REDACTED_PLACEHOLDER
    for output in d.get("outputs", []):
        if "target" in output and hasattr(output["target"], "value"):
            output["target"] = output["target"].value
        for redacted in _REDACTED_OUTPUT_FIELDS:
            if output.get(redacted):
                output[redacted] = _REDACTED_PLACEHOLDER
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
    # OpenAPI / Swagger / ReDoc are disabled by default — they leak job names,
    # webhook paths, and schedules to anyone who can reach the (localhost-bound)
    # port without auth. Operators who want them can opt in by setting
    # BOG_AGENTS_DAEMON_DOCS=1.
    import os as _os

    _expose_docs = _os.environ.get("BOG_AGENTS_DAEMON_DOCS", "").lower() in ("1", "true", "yes")
    app = FastAPI(
        title="Bog Agents Daemon",
        version=__version__,
        description="Ambient agent daemon REST API",
        docs_url="/docs" if _expose_docs else None,
        redoc_url="/redoc" if _expose_docs else None,
        openapi_url="/openapi.json" if _expose_docs else None,
    )
    webhook_tasks: set[asyncio.Task[Any]] = set()
    # Expose to lifecycle code (main.py) so shutdown can drain in-flight
    # webhook-triggered jobs alongside the scheduler's tracked tasks.
    app.state.webhook_tasks = webhook_tasks
    # Token holder is mutable so /admin/rotate-token can swap it at runtime.
    token_holder: dict[str, str] = {"value": token}

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Return daemon health and job count.

        Returns:
            Status dict with version and job count.
        """
        _check_auth(request, token_holder["value"])
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
        _check_auth(request, token_holder["value"])
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

    @app.post("/admin/rotate-token", status_code=200)
    async def rotate_token_endpoint(request: Request) -> dict[str, str]:
        """Rotate the daemon API token.

        The caller must authenticate with the *current* token. On success a
        new token is generated, persisted atomically (mode 0600) to the
        on-disk token file, and immediately becomes the only valid token —
        the old one is invalidated.

        Returns:
            Dict with ``token`` set to the new value. Callers must update
            any stored copy of the token; the old one will fail auth on
            the next request.
        """
        import os as _os
        import secrets
        import tempfile

        _check_auth(request, token_holder["value"])
        new_token = secrets.token_urlsafe(32)
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(_TOKEN_FILE.parent), suffix=".tmp")
        try:
            with _os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(new_token)
            Path(tmp_path).replace(_TOKEN_FILE)
            with contextlib.suppress(OSError):
                _TOKEN_FILE.chmod(0o600)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
        token_holder["value"] = new_token
        logger.info("daemon API token rotated")
        return {"token": new_token}

    # ------------------------------------------------------------------
    # Jobs collection
    # ------------------------------------------------------------------

    @app.get("/jobs")
    async def list_jobs_endpoint(request: Request) -> list[dict[str, Any]]:
        """List all configured jobs.

        Returns:
            List of job dicts.
        """
        _check_auth(request, token_holder["value"])
        return [_job_to_response(j) for j in load_jobs()]

    @app.post("/jobs", status_code=201)
    async def create_job_endpoint(request: Request, body: CreateJobRequest) -> dict[str, Any]:
        """Create a new ambient job.

        Args:
            body: The job creation parameters.

        Returns:
            The newly created job dict.
        """
        _check_auth(request, token_holder["value"])
        job = AmbientJob(
            name=body.name,
            description=body.description,
            prompt=body.prompt,
            pipeline_name=body.pipeline_name,
            skill_name=body.skill_name,
            model=body.model,
            working_dir=body.working_dir,
            max_retries=body.max_retries,
            retry_backoff_seconds=body.retry_backoff_seconds,
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
        _check_auth(request, token_holder["value"])
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return _job_to_response(job)

    @app.patch("/jobs/{job_id}")
    async def patch_job_endpoint(
        request: Request,
        job_id: str,
        body: UpdateJobRequest,
    ) -> dict[str, Any]:
        """Partially update an existing job.

        Only the fields the caller sends are overwritten — every other
        field on the stored record is preserved. Useful for edit-flow
        ergonomics from the CLI (``daemon jobs edit``) without forcing
        the user to round-trip the entire payload.
        """
        _check_auth(request, token_holder["value"])
        existing = get_job(job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        import dataclasses

        updates: dict[str, Any] = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.description is not None:
            updates["description"] = body.description
        if body.prompt is not None:
            updates["prompt"] = body.prompt
        if body.pipeline_name is not None:
            updates["pipeline_name"] = body.pipeline_name
        if body.skill_name is not None:
            updates["skill_name"] = body.skill_name
        if body.model is not None:
            updates["model"] = body.model
        if body.working_dir is not None:
            updates["working_dir"] = body.working_dir
        if body.max_retries is not None:
            updates["max_retries"] = body.max_retries
        if body.retry_backoff_seconds is not None:
            updates["retry_backoff_seconds"] = body.retry_backoff_seconds
        if body.triggers is not None:
            updates["triggers"] = [_trigger_config_from_model(t) for t in body.triggers]
        if body.outputs is not None:
            updates["outputs"] = [_output_config_from_model(o) for o in body.outputs]
        if body.enabled is not None:
            updates["enabled"] = body.enabled

        if not updates:
            return _job_to_response(existing)

        merged = dataclasses.replace(existing, **updates)
        upsert_job(merged)
        logger.info("Patched job %s (%s)", job_id, sorted(updates.keys()))
        return _job_to_response(merged)

    @app.delete("/jobs/{job_id}", status_code=204)
    async def delete_job_endpoint(request: Request, job_id: str) -> None:
        """Delete a job by ID.

        Args:
            job_id: The job identifier.

        Raises:
            HTTPException: 404 if not found.
        """
        _check_auth(request, token_holder["value"])
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
        _check_auth(request, token_holder["value"])
        job = get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        from bog_agents_daemon.models import JobRun, JobStatus
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

        # Route through the scheduler so the run honours the _running_jobs
        # overlap guard and the BOG_DAEMON_MAX_CONCURRENT_JOBS semaphore
        # instead of spawning an unbounded raw task. dispatch() reserves the
        # slot synchronously and returns the placeholder immediately; if the
        # job is already running it returns the placeholder marked SKIPPED.
        dispatched = scheduler.dispatch(
            job,
            trigger_type=TriggerType.MANUAL,
            trigger_context=context,
            existing_run=run,
        )
        if dispatched.status == JobStatus.SKIPPED:
            save_run(dispatched)
        return _run_to_response(dispatched)

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
        _check_auth(request, token_holder["value"])
        runs = list_runs(job_id=job_id)
        return [_run_to_response(r) for r in runs]

    @app.get("/runs")
    async def list_all_runs_endpoint(request: Request) -> list[dict[str, Any]]:
        """List recent runs across all jobs.

        Returns:
            List of up to 20 run dicts sorted newest-first.
        """
        _check_auth(request, token_holder["value"])
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
        _check_auth(request, token_holder["value"])
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
        _check_auth(request, token_holder["value"])
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

        from bog_agents_daemon.models import JobStatus

        _check_auth(request, token_holder["value"])
        try:
            payload = await request.json()
        except Exception:
            logger.warning("git-push payload was not valid JSON; treating as empty")
            payload = {}

        ref = payload.get("ref", "")
        # Normalise "refs/heads/main" → "main"
        branch = ref.split("/")[-1] if ref else ""
        # A missing/empty ref used to leave branch="" which fnmatch("", "*")
        # matches — firing every wildcard GIT_PUSH job on a malformed payload.
        # Reject the push instead of silently triggering on noise.
        if not branch:
            logger.warning("git-push payload had no usable 'ref'; ignoring (no jobs triggered)")
            return {"triggered": [], "count": 0}

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
                # Route through the scheduler so concurrent pushes honour the
                # overlap guard + concurrency semaphore (a push storm can no
                # longer fan out unbounded parallel runs of the same job).
                dispatched = scheduler.dispatch(
                    job,
                    trigger_type=TriggerType.GIT_PUSH,
                    trigger_context={"ref": ref, "branch": branch, **payload},
                )
                if dispatched.status != JobStatus.SKIPPED:
                    triggered.append(job.job_id)
                break

        return {"triggered": triggered, "count": len(triggered)}

    @app.post("/webhooks/github")
    async def receive_github(request: Request) -> dict[str, Any]:
        """Handle a GitHub webhook (Assign-to-bog, #30).

        Parses the event via `github_events.parse_github_event` and, when it is
        actionable (issue assigned/labeled, comment, CI failure), dispatches
        every enabled job with a GITHUB trigger — passing the parsed event as
        trigger_context so the agent can open a draft PR, revise, or repair.

        Auth (fail closed): a repo-level GitHub webhook can't send the daemon
        token, so the `X-Hub-Signature-256` HMAC is verified against
        `BOG_DAEMON_GITHUB_WEBHOOK_SECRET`. A valid daemon token is also accepted
        (the in-process test path). With neither a token nor a configured
        secret, the request is refused.
        """
        import json as _json
        import os as _os

        from bog_agents_daemon.github_events import parse_github_event
        from bog_agents_daemon.models import JobStatus

        raw_body = await request.body()
        provided_token = request.headers.get("X-Daemon-Token", "")
        is_token_authed = bool(provided_token) and hmac.compare_digest(provided_token, token_holder["value"])
        if not is_token_authed:
            secret = _os.environ.get("BOG_DAEMON_GITHUB_WEBHOOK_SECRET", "")
            if not secret:
                logger.warning("Refusing GitHub webhook: no BOG_DAEMON_GITHUB_WEBHOOK_SECRET configured")
                return {"triggered": [], "count": 0}
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                logger.warning("GitHub webhook signature mismatch")
                return {"triggered": [], "count": 0}

        event_type = request.headers.get("X-GitHub-Event", "")
        try:
            payload = _json.loads(raw_body) if raw_body else {}
        except (ValueError, TypeError):
            payload = {}

        event = parse_github_event(
            event_type,
            payload if isinstance(payload, dict) else {},
            bot_login=_os.environ.get("BOG_DAEMON_GITHUB_BOT_LOGIN", ""),
            trigger_label=_os.environ.get("BOG_DAEMON_GITHUB_TRIGGER_LABEL", ""),
        )
        if event is None:
            return {"triggered": [], "count": 0, "actionable": False}

        ctx = {
            "github_event": event.kind,
            "number": event.number,
            "title": event.title,
            "body": event.body,
            "branch": event.branch,
            "repo": event.repo,
            "actor": event.actor,
        }
        jobs = load_jobs()
        triggered: list[str] = []
        for job in jobs:
            if not job.enabled:
                continue
            if any(t.type == TriggerType.GITHUB for t in job.triggers):
                dispatched = scheduler.dispatch(job, trigger_type=TriggerType.GITHUB, trigger_context=ctx)
                if dispatched.status != JobStatus.SKIPPED:
                    triggered.append(job.job_id)
        return {"triggered": triggered, "count": len(triggered), "kind": event.kind}

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
        # Auth model for /webhooks/{path}: external services (GitHub, Slack,
        # CI) cannot send the daemon's local-management bearer token, so we
        # gate inbound webhooks on the per-trigger HMAC `webhook_secret`
        # instead. We still accept a daemon-token request — that's how the
        # in-process CLI test harness fires webhooks — but we no longer
        # *require* it, which would have made external use impossible.
        #
        # Security contract — fail closed
        # -------------------------------
        # 1. If a valid daemon token is presented, the request is trusted
        #    and the HMAC check is skipped (CLI test path).
        # 2. If no token is presented AND the trigger has a non-empty
        #    ``webhook_secret``, the X-Hub-Signature-256 HMAC check is
        #    the sole guard; mismatch → trigger is silently skipped.
        # 3. If no token is presented AND the trigger has an empty
        #    ``webhook_secret``, the request is REJECTED for that trigger
        #    (a warning is logged for the operator). Empty secret means
        #    "misconfigured trigger", not "public endpoint" — the latter
        #    is too easy to misconfigure into an open RCE.
        # An earlier version of this comment block claimed empty secrets
        # were "public entry points"; that was always aspirational and
        # never matched the rejection logic below. See
        # tests/unit_tests/test_webhook_auth.py for the pinned behaviour.
        provided_token = request.headers.get("X-Daemon-Token", "")
        # DMN-2/v4: compare against the *current* token (token_holder), not the
        # captured `token` closure — otherwise /admin/rotate-token never
        # invalidates the leaked old token here and rejects the new one, while
        # /webhooks/git-push (which uses token_holder) rotates correctly.
        current_token = token_holder["value"]
        is_token_authed = bool(provided_token) and hmac.compare_digest(provided_token, current_token)
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
                # HMAC secret validation. The local CLI path bears a valid
                # daemon token, so we trust the caller and skip HMAC.
                # Otherwise the trigger MUST have a non-empty secret AND
                # the request must carry a matching signature. An empty
                # ``webhook_secret`` does not mean "public" — it means
                # misconfigured, so we reject those instead of silently
                # admitting unauthenticated callers.
                if not is_token_authed:
                    if not trigger.webhook_secret:
                        logger.warning(
                            "Refusing unauthenticated webhook for job %s trigger path %s: trigger has no webhook_secret",
                            job.job_id,
                            webhook_path,
                        )
                        break
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
                from bog_agents_daemon.models import JobStatus

                # Route through the scheduler so an unauthenticated-but-valid
                # HMAC webhook storm cannot fan out unbounded parallel runs:
                # the overlap guard + concurrency semaphore apply.
                dispatched = scheduler.dispatch(
                    job,
                    trigger_type=TriggerType.WEBHOOK,
                    trigger_context={"path": webhook_path, "payload": payload},
                )
                if dispatched.status != JobStatus.SKIPPED:
                    triggered.append(job.job_id)
                break

        return {"triggered": triggered, "count": len(triggered)}

    return app
