"""Persistent background jobs manager with file-based tracking and notifications.

Extends BackgroundAgentManager with:
- Job state persisted to .bog-agents/jobs/<job_id>.json
- Desktop notifications on completion (via plyer, optional)
- Git worktree per job for isolation
- Webhook POST on completion (if WEBHOOK_URL configured)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from bog_agents_cli.background_agents import (
    BackgroundAgentManager,
    BackgroundStatus,
    BackgroundTask,
)

logger = logging.getLogger(__name__)

_JOBS_DIR = ".bog-agents/jobs"
_NOTIF_TITLE = "Bog Agents"


def _notify_desktop(title: str, message: str) -> None:
    """Send a desktop notification via plyer if available.

    Silently ignores ``ImportError`` and any platform-specific errors so the
    caller never needs to guard against notification failures.

    Args:
        title: Notification title.
        message: Notification body text.
    """
    try:
        from plyer import notification  # type: ignore[import-untyped]

        notification.notify(
            title=title,
            message=message,
            app_name="Bog Agents",
            timeout=8,
        )
    except Exception:
        logger.debug("Desktop notification failed", exc_info=True)


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    """POST a JSON payload to a webhook URL.

    Failures are logged as warnings and never propagated, so webhook errors
    cannot crash the calling code path.

    Args:
        url: Destination URL.
        payload: Dictionary that will be serialised as JSON.
    """
    try:
        import urllib.request

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        logger.warning("Webhook POST to %s failed: %s", url, exc)


def _task_to_dict(task: BackgroundTask) -> dict[str, Any]:
    """Serialise a BackgroundTask to a plain dictionary for JSON persistence.

    Args:
        task: The task to serialise.

    Returns:
        Dictionary containing the task's public fields.
    """
    result_preview: str | None = None
    if task.result:
        result_preview = task.result[:200]
    return {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "label": task.label,
        "status": str(task.status),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "result_preview": result_preview,
        "error": task.error,
        "worktree_branch": task.worktree_branch,
        "working_dir": task.working_dir,
    }


class PersistentJobsManager(BackgroundAgentManager):
    """Background agent manager with file-based persistence, notifications, and worktrees.

    Extends `BackgroundAgentManager` to:

    - Persist every task as a JSON file under ``.bog-agents/jobs/``.
    - Optionally send desktop notifications via ``plyer`` when a task
      transitions to a terminal state.
    - Optionally create a git worktree branch per task for file isolation.
    - Optionally POST a JSON webhook when a task completes.

    Previously persisted terminal-state jobs are loaded on construction so
    historical results survive process restarts.

    Args:
        project_root: Project root directory for job file storage.
            Defaults to ``Path.cwd()``.
        webhook_url: Optional URL to POST completion events to.
        enable_worktrees: When ``True``, auto-generate a ``bog-job-*`` branch
            name for each submitted task (passed as ``worktree_branch``).
        enable_notifications: When ``True``, send a desktop notification on
            task completion via ``plyer``.
        **kwargs: Forwarded to `BackgroundAgentManager.__init__`.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        webhook_url: str | None = None,
        enable_worktrees: bool = True,
        enable_notifications: bool = True,
        **kwargs: Any,
    ) -> None:
        # Wire our hook before super().__init__ so the callback is available
        # immediately if super raises (unlikely but safe).
        super().__init__(on_complete=self._on_task_complete_hook, **kwargs)
        self._project_root: Path = project_root or Path.cwd()
        self._webhook_url: str | None = webhook_url
        self._enable_worktrees: bool = enable_worktrees
        self._enable_notifications: bool = enable_notifications
        self._load_persisted_jobs()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _jobs_dir(self) -> Path:
        """Return the jobs directory, creating it on first access.

        Returns:
            Absolute path to the ``.bog-agents/jobs`` directory.
        """
        path = self._project_root / _JOBS_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _job_path(self, task_id: str) -> Path:
        """Return the JSON file path for a given task.

        Args:
            task_id: Task identifier.

        Returns:
            Path to ``<jobs_dir>/<task_id>.json``.
        """
        return self._jobs_dir() / f"{task_id}.json"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_task(self, task: BackgroundTask) -> None:
        """Write a task's current state to its JSON file.

        Args:
            task: The task to persist.
        """
        try:
            from bog_agents_cli.io_utils import atomic_write_text

            data = _task_to_dict(task)
            atomic_write_text(self._job_path(task.task_id), json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to persist task %s: %s", task.task_id, exc)

    def _load_persisted_jobs(self) -> None:
        """Load terminal-state jobs from disk into the in-memory task registry.

        Only jobs whose status is ``completed``, ``failed``, or ``cancelled``
        are loaded, because in-progress jobs from a previous process cannot be
        resumed automatically.
        """
        terminal_statuses = {
            BackgroundStatus.COMPLETED,
            BackgroundStatus.FAILED,
            BackgroundStatus.CANCELLED,
        }
        try:
            job_paths = list(self._jobs_dir().glob("*.json"))
        except OSError as exc:
            logger.warning("Could not read jobs directory: %s", exc)
            return
        for path in job_paths:
            try:
                data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                status_str = data.get("status", "")
                # Skip non-terminal or already in-memory tasks.
                try:
                    status = BackgroundStatus(status_str)
                except ValueError:
                    logger.debug(
                        "Unknown status '%s' in %s — skipping", status_str, path
                    )
                    continue
                if status not in terminal_statuses:
                    continue
                task_id = data.get("task_id", path.stem)
                if task_id in self._tasks:
                    continue
                task = BackgroundTask(
                    task_id=task_id,
                    prompt=data.get("prompt", ""),
                    label=data.get("label", ""),
                    status=status,
                    created_at=float(data.get("created_at", time.time())),
                    started_at=data.get("started_at"),
                    completed_at=data.get("completed_at"),
                    result=data.get("result_preview"),
                    error=data.get("error"),
                    worktree_branch=data.get("worktree_branch"),
                    working_dir=data.get("working_dir"),
                )
                self._tasks[task_id] = task
            except (json.JSONDecodeError, KeyError, ValueError, OSError):
                logger.debug("Skipping unreadable job file: %s", path)

    # ------------------------------------------------------------------
    # Submit override
    # ------------------------------------------------------------------

    async def submit(  # type: ignore[override]
        self,
        prompt: str,
        *,
        worktree_branch: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Submit a background task, optionally auto-assigning a worktree branch.

        If `enable_worktrees` is ``True`` and no explicit ``worktree_branch``
        is provided, a branch name of the form ``bog-job-<hex8>`` is generated
        and forwarded to the parent `submit`.

        Args:
            prompt: Task prompt.
            worktree_branch: Optional git branch name for worktree isolation.
                Auto-generated when ``enable_worktrees=True`` and not supplied.
            **kwargs: Forwarded to `BackgroundAgentManager.submit`.

        Returns:
            Task ID string.
        """
        if self._enable_worktrees and not worktree_branch:
            worktree_branch = f"bog-job-{uuid.uuid4().hex[:8]}"

        task_id = await super().submit(
            prompt, worktree_branch=worktree_branch, **kwargs
        )

        task = self._tasks.get(task_id)
        if task is not None:
            self._persist_task(task)

        return task_id

    # ------------------------------------------------------------------
    # Completion hook
    # ------------------------------------------------------------------

    def _on_task_complete_hook(self, task: BackgroundTask) -> None:
        """Handle task completion: persist, notify, and post webhook.

        This is wired as the ``on_complete`` callback in `__init__` so it
        fires after every task transitions to a terminal state.

        Args:
            task: The completed (or failed/cancelled) task.
        """
        self._persist_task(task)

        if self._enable_notifications:
            status_label = str(task.status)
            body = ""
            if task.result:
                body = task.result[:100]
            elif task.error:
                body = task.error
            _notify_desktop(f"Job {task.task_id} {status_label}", body)

        if self._webhook_url:
            _post_webhook(
                self._webhook_url,
                {
                    "task_id": task.task_id,
                    "status": str(task.status),
                    "label": task.label,
                },
            )

    # ------------------------------------------------------------------
    # Disk-based listing helpers
    # ------------------------------------------------------------------

    def list_jobs_from_disk(self) -> list[dict[str, Any]]:
        """Load all job records from disk and return as raw dictionaries.

        Returns:
            List of job dictionaries sorted by ``created_at`` descending.
        """
        records: list[dict[str, Any]] = []
        for path in self._jobs_dir().glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(data)
            except (json.JSONDecodeError, OSError):
                logger.debug("Skipping unreadable job file: %s", path)
        records.sort(key=lambda d: d.get("created_at", 0.0), reverse=True)
        return records

    def format_jobs_table(self) -> str:
        """Render a combined in-memory + on-disk jobs table.

        In-memory tasks take precedence; additional jobs found only on disk are
        appended.  Columns: ID | status | label | branch | duration.

        Returns:
            Formatted table string.
        """
        # Build de-duplicated list: in-memory first, then disk-only.
        seen_ids: set[str] = set()
        rows: list[dict[str, Any]] = []

        for task in self.all_tasks:
            seen_ids.add(task.task_id)
            dur = task.duration_seconds
            rows.append(
                {
                    "task_id": task.task_id,
                    "status": str(task.status),
                    "label": task.label,
                    "branch": task.worktree_branch or "",
                    "duration": dur,
                }
            )

        for data in self.list_jobs_from_disk():
            task_id = data.get("task_id", "")
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            started = data.get("started_at")
            completed = data.get("completed_at")
            dur: float | None = None
            if started is not None:
                dur = (completed or time.time()) - started
            rows.append(
                {
                    "task_id": task_id,
                    "status": data.get("status", "unknown"),
                    "label": data.get("label", ""),
                    "branch": data.get("worktree_branch") or "",
                    "duration": dur,
                }
            )

        if not rows:
            return "No jobs found."

        w_id = 12
        w_status = 10
        w_label = 24
        w_branch = 22
        w_dur = 8

        header = (
            f"{'ID':<{w_id}}  {'STATUS':<{w_status}}  {'LABEL':<{w_label}}  "
            f"{'BRANCH':<{w_branch}}  {'DURATION':>{w_dur}}"
        )
        sep = "-" * len(header)
        lines = [header, sep]

        for row in rows:
            task_id = str(row["task_id"])[:w_id]
            status = str(row["status"])[:w_status]
            label = str(row["label"])
            label = (
                label[:w_label]
                if len(label) <= w_label
                else label[: w_label - 3] + "..."
            )
            branch = str(row["branch"])
            branch = (
                branch[:w_branch]
                if len(branch) <= w_branch
                else branch[: w_branch - 3] + "..."
            )
            dur = row["duration"]
            dur_str = f"{dur:.0f}s" if dur is not None else "-"
            lines.append(
                f"{task_id:<{w_id}}  {status:<{w_status}}  {label:<{w_label}}  "
                f"{branch:<{w_branch}}  {dur_str:>{w_dur}}"
            )

        lines.append(f"\n{len(rows)} job(s) shown.")
        return "\n".join(lines)
