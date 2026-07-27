"""Data models for the ambient agent daemon."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TriggerType(StrEnum):
    """Types of triggers that can initiate an ambient job."""

    CRON = "cron"
    FILE_CHANGE = "file_change"
    WEBHOOK = "webhook"
    GIT_PUSH = "git_push"
    GITHUB = "github"  # issue-assigned/labeled, comment, CI-failure (#30)
    MANUAL = "manual"
    INTERVAL = "interval"


class JobStatus(StrEnum):
    """Lifecycle states for an ambient job or job run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class OutputTarget(StrEnum):
    """Destinations where job output can be delivered."""

    FILE = "file"
    EMAIL = "email"
    SLACK = "slack"
    GITHUB_COMMENT = "github_comment"
    WEBHOOK = "webhook"
    STDOUT = "stdout"
    LOG = "log"


@dataclass
class TriggerConfig:
    """Configuration for a single job trigger.

    Attributes:
        type: The trigger mechanism.
        cron: Cron expression e.g. "0 9 * * 1-5" (used when type=CRON).
        interval_seconds: Seconds between runs (used when type=INTERVAL).
        watch_patterns: Glob patterns to watch (used when type=FILE_CHANGE).
        watch_dir: Directory to watch for file changes.
        debounce_seconds: Seconds to wait before firing after a file change.
        webhook_path: URL path suffix e.g. "/hooks/my-hook" (used when type=WEBHOOK).
        webhook_secret: HMAC secret for webhook payload verification.
        git_branch_pattern: Branch filter for git_push trigger (glob pattern).
    """

    type: TriggerType
    # cron: cron expression e.g. "0 9 * * 1-5"
    cron: str = ""
    # interval: seconds between runs
    interval_seconds: int = 0
    # file_change: glob patterns
    watch_patterns: list[str] = field(default_factory=list)
    watch_dir: str = ""
    debounce_seconds: float = 5.0
    # webhook: path suffix e.g. "/hooks/my-hook"
    webhook_path: str = ""
    webhook_secret: str = ""
    # git_push: branch filter
    git_branch_pattern: str = "*"


@dataclass
class OutputConfig:
    """Configuration for a single output destination.

    Attributes:
        target: The output delivery mechanism.
        file_path: Filesystem path for file output.
        append: If True, append to file_path; otherwise overwrite.
        smtp_host: SMTP server hostname for email output.
        smtp_port: SMTP server port.
        smtp_username: SMTP authentication username.
        smtp_password: SMTP authentication password.
        from_addr: Sender email address.
        to_addrs: Recipient email addresses.
        subject_template: Email subject template with {job_name} substitution.
        slack_webhook_url: Slack incoming webhook URL.
        slack_channel: Slack channel override (optional).
        github_repo: GitHub repo in "owner/repo" format.
        github_issue_or_pr: Issue or PR number for GitHub comment output.
        webhook_url: URL to POST output to.
        webhook_headers: Additional HTTP headers for webhook requests.
    """

    target: OutputTarget
    # file output
    file_path: str = ""
    append: bool = True
    # email
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    subject_template: str = "Bog Agents: {job_name} completed"
    # slack
    slack_webhook_url: str = ""
    slack_channel: str = ""
    # github
    github_repo: str = ""
    github_issue_or_pr: int = 0
    github_token: str = ""
    # webhook
    webhook_url: str = ""
    webhook_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class AmbientJob:
    """A named recurring agent task managed by the daemon.

    Attributes:
        job_id: Unique identifier for the job (auto-generated).
        name: Human-readable job name.
        description: Optional description of what the job does.
        prompt: The prompt to send to the agent.
        pipeline_name: Name of a saved pipeline to run instead of a raw prompt.
        skill_name: Name of a skill to invoke instead of a raw prompt.
        model: Model identifier override (uses daemon default if empty).
        working_dir: Working directory for the agent (uses cwd if empty).
        max_retries: Extra attempts on a transient failure (agent invocation
            and each output dispatch). 0 (default) preserves single-shot
            behaviour; total attempts = max_retries + 1.
        retry_backoff_seconds: Base delay before the first retry; doubles each
            subsequent attempt (exponential backoff).
        triggers: One or more trigger configurations.
        outputs: One or more output delivery configurations.
        enabled: Whether the job is active and eligible for scheduling.
        last_run_at: Unix timestamp of last execution.
        last_status: Status from the most recent run.
        last_output: Truncated output from the most recent run.
        run_count: Total number of times this job has been executed.
        created_at: Unix timestamp when the job was created.
    """

    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    description: str = ""
    # What to run
    prompt: str = ""
    pipeline_name: str = ""
    skill_name: str = ""
    model: str = ""
    working_dir: str = ""
    # Retry policy (opt-in; 0 retries = prior single-shot behaviour)
    max_retries: int = 0
    retry_backoff_seconds: float = 2.0
    # When to run
    triggers: list[TriggerConfig] = field(default_factory=list)
    # Where to send output
    outputs: list[OutputConfig] = field(default_factory=list)
    # State
    enabled: bool = True
    last_run_at: float = 0.0
    last_status: JobStatus = JobStatus.PENDING
    last_output: str = ""
    run_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class JobRun:
    """A single execution record for an AmbientJob.

    Attributes:
        run_id: Unique identifier for this run (auto-generated).
        job_id: Identifier of the parent AmbientJob.
        job_name: Name of the parent AmbientJob (denormalized for display).
        started_at: Unix timestamp when execution began.
        finished_at: Unix timestamp when execution completed (0 if still running).
        status: Current execution state.
        output: Captured output from the agent.
        error: Error message if the run failed.
        trigger_type: How this run was initiated.
        trigger_context: Additional context from the trigger (e.g. webhook payload).
    """

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str = ""
    job_name: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    status: JobStatus = JobStatus.RUNNING
    output: str = ""
    error: str = ""
    trigger_type: TriggerType = TriggerType.MANUAL
    trigger_context: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    """Number of agent invocation attempts made for this run.

    1 for a first-try success or a job with no retry policy; higher when
    `AmbientJob.max_retries` triggered one or more retries.
    """
    dispatch_errors: list[dict[str, str]] = field(default_factory=list)
    """Per-target dispatch failures captured after the agent finished.

    Each entry is ``{"target": "<email|slack|...>", "error": "..."}``.
    The agent run can succeed while one or more output targets fail
    (e.g. transient SMTP outage). Previously these were logged-and-
    forgotten which meant operators could not tell from the run record
    that delivery never happened.
    """
