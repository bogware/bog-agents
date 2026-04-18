"""Background jobs middleware — persistent job tracking in .bog-agents/jobs/.

Jobs are stored as JSON files in .bog-agents/jobs/<job_id>.json and persist
across process restarts. The agent gains tools to submit, monitor, and cancel
background jobs programmatically.

Job file format:
    {
      "job_id": "job-abc123",
      "prompt": "Implement feature X",
      "label": "feature-x",
      "status": "running",
      "created_at": 1234567890.0,
      "started_at": 1234567890.0,
      "completed_at": null,
      "branch": "bog-job-abc123",
      "result_preview": null,
      "error": null
    }
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

_JOBS_DIR = ".bog-agents/jobs"

# Valid job statuses.
_STATUS_QUEUED = "queued"
_STATUS_RUNNING = "running"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"
_STATUS_CANCELLED = "cancelled"

_TERMINAL_STATUSES = frozenset({_STATUS_COMPLETED, _STATUS_FAILED, _STATUS_CANCELLED})


@dataclass
class JobRecord:
    """A persistent background job record.

    Attributes:
        job_id: Unique job identifier (e.g. ``job-abc123``).
        prompt: Task prompt passed to the agent.
        label: Short human-readable label.
        status: Current lifecycle status — ``queued``, ``running``,
            ``completed``, ``failed``, or ``cancelled``.
        created_at: Unix timestamp when the job was created.
        started_at: Unix timestamp when the job started running, or ``None``.
        completed_at: Unix timestamp when the job finished, or ``None``.
        branch: Associated git branch name (e.g. ``bog-job-abc123``).
        result_preview: First 200 chars of result output, or ``None``.
        error: Error message if the job failed, or ``None``.
    """

    job_id: str
    prompt: str
    label: str = ""
    status: str = _STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    branch: str = ""
    result_preview: str | None = None
    error: str | None = None

    @property
    def duration_secs(self) -> float | None:
        """Elapsed time in seconds between start and completion.

        Returns:
            Duration in seconds, or ``None`` if the job has not started.
        """
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict[str, Any]:
        """Serialise the record to a plain dictionary.

        Returns:
            Dictionary suitable for JSON serialisation.
        """
        return {
            "job_id": self.job_id,
            "prompt": self.prompt,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "branch": self.branch,
            "result_preview": self.result_preview,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRecord:
        """Deserialise a record from a plain dictionary.

        Args:
            data: Dictionary previously produced by `to_dict`.

        Returns:
            A new `JobRecord` instance.
        """
        return cls(
            job_id=data["job_id"],
            prompt=data.get("prompt", ""),
            label=data.get("label", ""),
            status=data.get("status", _STATUS_QUEUED),
            created_at=float(data.get("created_at", time.time())),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            branch=data.get("branch", ""),
            result_preview=data.get("result_preview"),
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _jobs_dir(project_root: Path) -> Path:
    """Return the jobs directory, creating it if necessary.

    Args:
        project_root: Root of the project (usually ``Path.cwd()``).

    Returns:
        Absolute path to the ``.bog-agents/jobs`` directory.
    """
    jobs_path = project_root / _JOBS_DIR
    jobs_path.mkdir(parents=True, exist_ok=True)
    return jobs_path


def save_job(project_root: Path, job: JobRecord) -> None:
    """Persist a job record to disk as a JSON file.

    Args:
        project_root: Root of the project.
        job: The job record to save.
    """
    target = _jobs_dir(project_root) / f"{job.job_id}.json"
    from bog_agents.utils.io import atomic_write_text
    atomic_write_text(target, json.dumps(job.to_dict(), indent=2))


def load_job(project_root: Path, job_id: str) -> JobRecord | None:
    """Load a single job record from disk.

    Args:
        project_root: Root of the project.
        job_id: Job identifier to load.

    Returns:
        The `JobRecord`, or ``None`` if the file is missing or corrupt.
    """
    target = _jobs_dir(project_root) / f"{job_id}.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return JobRecord.from_dict(data)
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def load_all_jobs(project_root: Path, *, limit: int = 50) -> list[JobRecord]:
    """Load all persisted job records, newest first.

    Args:
        project_root: Root of the project.
        limit: Maximum number of records to return.

    Returns:
        List of `JobRecord` objects sorted by ``created_at`` descending,
        capped at ``limit``.
    """
    jobs_path = _jobs_dir(project_root)
    records: list[JobRecord] = []
    for path in jobs_path.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(JobRecord.from_dict(data))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            logger.debug("Skipping unreadable job file: %s", path)
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records[:limit]


def make_job_id() -> str:
    """Generate a new unique job identifier.

    Returns:
        A string of the form ``job-<8 hex chars>``.
    """
    return "job-" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_COL_WIDTHS = {
    "job_id": 12,
    "status": 10,
    "preview": 30,
    "branch": 22,
    "duration": 8,
}


def format_job_table(jobs: list[JobRecord]) -> str:
    """Render a multi-line status table for a list of jobs.

    Columns: job_id | status | label/prompt preview | branch | duration.

    Args:
        jobs: List of job records to render.

    Returns:
        A human-readable table string.
    """
    if not jobs:
        return "No background jobs found."

    header = (
        f"{'JOB ID':<{_COL_WIDTHS['job_id']}}  "
        f"{'STATUS':<{_COL_WIDTHS['status']}}  "
        f"{'LABEL / PROMPT':<{_COL_WIDTHS['preview']}}  "
        f"{'BRANCH':<{_COL_WIDTHS['branch']}}  "
        f"{'DURATION':>{_COL_WIDTHS['duration']}}"
    )
    separator = "-" * len(header)
    lines = [header, separator]

    for job in jobs:
        preview_src = job.label or job.prompt
        preview = preview_src[:30] if len(preview_src) <= 30 else preview_src[:27] + "..."
        branch = job.branch[:22] if len(job.branch) <= 22 else job.branch[:19] + "..."
        dur = job.duration_secs
        dur_str = f"{dur:.0f}s" if dur is not None else "-"
        lines.append(
            f"{job.job_id:<{_COL_WIDTHS['job_id']}}  "
            f"{job.status:<{_COL_WIDTHS['status']}}  "
            f"{preview:<{_COL_WIDTHS['preview']}}  "
            f"{branch:<{_COL_WIDTHS['branch']}}  "
            f"{dur_str:>{_COL_WIDTHS['duration']}}"
        )

    lines.append(f"\n{len(jobs)} job(s) shown.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class BackgroundJobsState(TypedDict):
    """LangGraph state extension for the background jobs middleware (empty)."""


class BackgroundJobsMiddleware(AgentMiddleware[BackgroundJobsState, ContextT, ResponseT]):
    """Middleware that exposes persistent background job management tools.

    Jobs are stored as JSON files under ``.bog-agents/jobs/`` in the project
    root and survive process restarts.  The agent receives four tools:

    - ``list_jobs`` — show recent jobs in a table.
    - ``job_status`` — inspect a single job by ID.
    - ``cancel_job`` — mark a job as cancelled.
    - ``create_job_record`` — register a new job (status ``queued``).

    Args:
        project_root: Root directory for job storage. Defaults to ``Path.cwd()``.
    """

    state_schema = BackgroundJobsState

    def __init__(self, *, project_root: Path | None = None) -> None:
        self._project_root: Path = project_root or Path.cwd()
        self._tools = self._build_tools()

    @property
    def tools(self) -> list[BaseTool]:
        """Expose background job tools to the agent."""
        return self._tools

    def _build_tools(self) -> list[BaseTool]:
        """Construct the four background-job management tools.

        Returns:
            List of `BaseTool` instances.
        """
        mw = self

        def list_jobs(
            limit: Annotated[int, "Max jobs to show"] = 20,
        ) -> str:
            """List recent background jobs and their statuses."""
            jobs = load_all_jobs(mw._project_root, limit=limit)
            return format_job_table(jobs)

        def job_status(
            job_id: Annotated[str, "Job ID to check"],
        ) -> str:
            """Return detailed status information for a single background job."""
            job = load_job(mw._project_root, job_id)
            if job is None:
                return f"Job '{job_id}' not found in {mw._project_root / _JOBS_DIR}."
            dur = job.duration_secs
            dur_str = f"{dur:.1f}s" if dur is not None else "not started"
            lines = [
                f"Job ID:      {job.job_id}",
                f"Status:      {job.status}",
                f"Label:       {job.label or '(none)'}",
                f"Branch:      {job.branch or '(none)'}",
                f"Created:     {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(job.created_at))}",
                f"Duration:    {dur_str}",
                f"Prompt:      {job.prompt[:120]}",
            ]
            if job.result_preview:
                lines.append(f"Result:      {job.result_preview[:200]}")
            if job.error:
                lines.append(f"Error:       {job.error[:200]}")
            return "\n".join(lines)

        def cancel_job(
            job_id: Annotated[str, "Job ID to cancel"],
        ) -> str:
            """Cancel a background job by marking its status as cancelled on disk."""
            job = load_job(mw._project_root, job_id)
            if job is None:
                return f"Job '{job_id}' not found."
            if job.status in _TERMINAL_STATUSES:
                return f"Job '{job_id}' is already in terminal state '{job.status}' — cannot cancel."
            job.status = _STATUS_CANCELLED
            job.completed_at = time.time()
            save_job(mw._project_root, job)
            return f"Job '{job_id}' has been cancelled."

        def create_job_record(
            prompt: Annotated[str, "Task prompt"],
            label: Annotated[str, "Short label"] = "",
            branch: Annotated[str, "Git branch name"] = "",
        ) -> str:
            """Register a new background job record with status 'queued'.

            Returns the job ID and the path to the persisted JSON file so the
            caller can wire up an actual worker process.
            """
            job_id = make_job_id()
            if not branch:
                branch = f"bog-job-{job_id[4:]}"
            job = JobRecord(
                job_id=job_id,
                prompt=prompt,
                label=label,
                status=_STATUS_QUEUED,
                branch=branch,
            )
            save_job(mw._project_root, job)
            job_path = _jobs_dir(mw._project_root) / f"{job_id}.json"
            return f"Created job {job_id}\nFile: {job_path}"

        return [
            StructuredTool.from_function(
                name="list_jobs",
                description="List recent background jobs and their statuses.",
                func=list_jobs,
            ),
            StructuredTool.from_function(
                name="job_status",
                description="Return detailed status for a single background job.",
                func=job_status,
            ),
            StructuredTool.from_function(
                name="cancel_job",
                description="Cancel a background job by marking it cancelled on disk.",
                func=cancel_job,
            ),
            StructuredTool.from_function(
                name="create_job_record",
                description="Register a new queued background job and return its ID.",
                func=create_job_record,
            ),
        ]
