"""Background agents -- fire-and-forget long-running agent tasks.

Run agent tasks in the background and get notified when they complete.
Supports multiple concurrent background agents with status tracking.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_PROMPT_PREVIEW_LEN = 50


class BackgroundStatus(StrEnum):
    """Status of a background agent."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = frozenset({
    BackgroundStatus.COMPLETED,
    BackgroundStatus.FAILED,
    BackgroundStatus.CANCELLED,
})


@dataclass
class BackgroundTask:
    """A background agent task."""

    task_id: str
    prompt: str
    status: BackgroundStatus = BackgroundStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: str | None = None
    error: str | None = None
    model: str | None = None
    working_dir: str | None = None
    _task: asyncio.Task[Any] | None = field(
        default=None, repr=False
    )

    @property
    def duration_seconds(self) -> float | None:
        """Task duration in seconds, or ``None`` if not started."""
        if self.started_at is None:
            return None
        end = self.completed_at or time.time()
        return end - self.started_at

    @property
    def status_line(self) -> str:
        """One-line status summary."""
        duration = ""
        if self.duration_seconds is not None:
            duration = f" ({self.duration_seconds:.0f}s)"
        prompt_preview = (
            self.prompt[:_PROMPT_PREVIEW_LEN] + "..."
            if len(self.prompt) > _PROMPT_PREVIEW_LEN
            else self.prompt
        )
        return (
            f"[{self.task_id}] {self.status}"
            f"{duration}: {prompt_preview}"
        )


class BackgroundAgentManager:
    """Manages background agent tasks.

    Example:
        ```python
        manager = BackgroundAgentManager(
            agent_factory=create_my_agent,
        )
        task_id = await manager.submit("Fix all lint errors")
        status = manager.get_status(task_id)
        ```
    """

    def __init__(
        self,
        *,
        agent_factory: Callable[[], Any] | None = None,
        max_concurrent: int = 5,
        on_complete: Callable[[BackgroundTask], None] | None = None,
    ) -> None:
        """Initialize the background agent manager.

        Args:
            agent_factory: Callable that creates a new agent instance.
            max_concurrent: Maximum concurrent background tasks.
            on_complete: Callback(task) when a task finishes.
        """
        self._tasks: dict[str, BackgroundTask] = {}
        self._agent_factory = agent_factory
        self._max_concurrent = max_concurrent
        self._on_complete = on_complete
        self._counter = 0

    def _next_id(self) -> str:
        """Generate the next task ID.

        Returns:
            A unique task ID string.
        """
        self._counter += 1
        return f"bg-{self._counter:03d}"

    @property
    def running_count(self) -> int:
        """Number of currently running tasks."""
        return sum(
            1
            for t in self._tasks.values()
            if t.status == BackgroundStatus.RUNNING
        )

    @property
    def all_tasks(self) -> list[BackgroundTask]:
        """All tasks sorted by creation time."""
        return sorted(
            self._tasks.values(), key=lambda t: t.created_at
        )

    async def submit(
        self,
        prompt: str,
        *,
        model: str | None = None,
        working_dir: str | None = None,
    ) -> str:
        """Submit a new background agent task.

        Args:
            prompt: The task prompt for the agent.
            model: Optional model override.
            working_dir: Optional working directory.

        Returns:
            Task ID.

        Raises:
            RuntimeError: If max concurrent tasks reached or no factory.
        """
        if self.running_count >= self._max_concurrent:
            msg = (
                f"Maximum concurrent background tasks "
                f"({self._max_concurrent}) reached. "
                "Wait for a task to complete or cancel one."
            )
            raise RuntimeError(msg)

        task_id = self._next_id()
        task = BackgroundTask(
            task_id=task_id,
            prompt=prompt,
            model=model,
            working_dir=working_dir,
        )
        self._tasks[task_id] = task

        async_task = asyncio.create_task(self._run_task(task))
        task._task = async_task

        logger.info(
            "Background task %s submitted: %s",
            task_id,
            prompt[:80],
        )
        return task_id

    async def _run_task(self, task: BackgroundTask) -> None:
        """Execute a background task.

        Args:
            task: The task to run.

        Raises:
            RuntimeError: If no agent factory is configured.
        """
        task.status = BackgroundStatus.RUNNING
        task.started_at = time.time()

        try:
            if self._agent_factory is None:
                msg = "No agent_factory configured"
                raise RuntimeError(msg)  # noqa: TRY301

            agent = self._agent_factory()
            config = {
                "configurable": {"thread_id": f"bg-{task.task_id}"}
            }
            input_data = {
                "messages": [
                    {"role": "user", "content": task.prompt}
                ],
            }

            result = await agent.ainvoke(input_data, config=config)

            messages = result.get("messages", [])
            response = ""
            for msg in reversed(messages):
                if (
                    hasattr(msg, "content")
                    and getattr(msg, "type", None) == "ai"
                ):
                    response = msg.content
                    break
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "assistant"
                ):
                    response = msg.get("content", "")
                    break

            task.result = response
            task.status = BackgroundStatus.COMPLETED
            task.completed_at = time.time()
            logger.info(
                "Background task %s completed in %.1fs",
                task.task_id,
                task.duration_seconds or 0,
            )
        except asyncio.CancelledError:
            task.status = BackgroundStatus.CANCELLED
            task.completed_at = time.time()
            logger.info(
                "Background task %s cancelled", task.task_id
            )
        except Exception:
            task.status = BackgroundStatus.FAILED
            task.completed_at = time.time()
            logger.exception(
                "Background task %s failed", task.task_id
            )

        if self._on_complete:
            try:
                self._on_complete(task)
            except Exception:
                logger.debug(
                    "on_complete callback failed", exc_info=True
                )

    def get_status(self, task_id: str) -> BackgroundTask | None:
        """Get status of a background task.

        Args:
            task_id: Task ID.

        Returns:
            ``BackgroundTask`` or ``None`` if not found.
        """
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """Cancel a running background task.

        Args:
            task_id: Task ID to cancel.

        Returns:
            ``True`` if the task was found and cancel was requested.
        """
        task = self._tasks.get(task_id)
        if task is None or task._task is None:
            return False
        if task.status == BackgroundStatus.RUNNING:
            task._task.cancel()
            return True
        return False

    def get_completed(self) -> list[BackgroundTask]:
        """Get all completed tasks.

        Returns:
            List of completed tasks.
        """
        return [
            t
            for t in self._tasks.values()
            if t.status
            in {BackgroundStatus.COMPLETED, BackgroundStatus.FAILED}
        ]

    def format_status_table(self) -> str:
        """Format a status table of all tasks.

        Returns:
            Formatted status string.
        """
        if not self._tasks:
            return "No background tasks."

        lines = ["Background Tasks:", "-" * 60]
        lines.extend(task.status_line for task in self.all_tasks)

        running = self.running_count
        total = len(self._tasks)
        lines.append(f"\n{running} running, {total} total")
        return "\n".join(lines)

    def cleanup_completed(self) -> int:
        """Remove completed and failed tasks from tracking.

        Returns:
            Number of tasks removed.
        """
        to_remove = [
            tid
            for tid, task in self._tasks.items()
            if task.status in _TERMINAL_STATUSES
        ]
        for tid in to_remove:
            del self._tasks[tid]
        return len(to_remove)
