"""Scheduled Runs middleware for cron-like autonomous agent execution.

Supports scheduling recurring agent tasks with cron expressions or
simple interval specifications. Tasks run in background with results
persisted to the filesystem.

Example: "Every morning, check for dependency updates and open PRs"
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

logger = logging.getLogger(__name__)


class ScheduleStatus(StrEnum):
    """Status of a scheduled task."""

    ACTIVE = "active"
    PAUSED = "paused"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DISABLED = "disabled"


class IntervalUnit(StrEnum):
    """Time units for simple interval schedules."""

    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"


@dataclass
class ScheduleInterval:
    """A simple interval-based schedule."""

    value: int
    unit: IntervalUnit

    @property
    def seconds(self) -> float:
        """Convert to seconds."""
        multipliers = {
            IntervalUnit.MINUTES: 60,
            IntervalUnit.HOURS: 3600,
            IntervalUnit.DAYS: 86400,
        }
        return self.value * multipliers[self.unit]

    def __str__(self) -> str:
        """Return human-readable interval description."""
        return f"every {self.value} {self.unit}"


@dataclass
class CronExpression:
    """A cron-like schedule expression.

    Supports: minute hour day_of_month month day_of_week
    Uses standard cron syntax with * for wildcards.
    """

    minute: str = "*"
    hour: str = "*"
    day_of_month: str = "*"
    month: str = "*"
    day_of_week: str = "*"

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        """Parse a cron expression string.

        Args:
            expression: Space-separated cron expression (5 fields).

        Returns:
            Parsed CronExpression.

        Raises:
            ValueError: If expression is invalid.
        """
        parts = expression.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Cron expression must have 5 fields, got {len(parts)}: {expression}")
        return cls(
            minute=parts[0],
            hour=parts[1],
            day_of_month=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )

    def _field_matches(self, field_expr: str, value: int) -> bool:
        """Check if a single cron field matches a value."""
        if field_expr == "*":
            return True
        # Handle comma-separated values
        for part in field_expr.split(","):
            # Handle ranges (e.g., 1-5)
            if "-" in part:
                start, end = part.split("-", 1)
                if int(start) <= value <= int(end):
                    return True
            # Handle step values (e.g., */5)
            elif "/" in part:
                base, step = part.split("/", 1)
                step_val = int(step)
                if base == "*":
                    if value % step_val == 0:
                        return True
                elif value >= int(base) and (value - int(base)) % step_val == 0:
                    return True
            elif int(part) == value:
                return True
        return False

    def matches_time(self, timestamp: float | None = None) -> bool:
        """Check if the current time matches this cron expression.

        Args:
            timestamp: Unix timestamp to check. Uses current time if None.

        Returns:
            True if the time matches all cron fields.
        """
        import datetime

        dt = datetime.datetime.fromtimestamp(timestamp or time.time())
        return (
            self._field_matches(self.minute, dt.minute)
            and self._field_matches(self.hour, dt.hour)
            and self._field_matches(self.day_of_month, dt.day)
            and self._field_matches(self.month, dt.month)
            and self._field_matches(self.day_of_week, (dt.weekday() + 1) % 7)
        )

    def __str__(self) -> str:
        """Return cron expression string."""
        return f"{self.minute} {self.hour} {self.day_of_month} {self.month} {self.day_of_week}"


@dataclass
class ScheduledTask:
    """A scheduled agent task."""

    task_id: str
    """Unique task identifier."""

    name: str
    """Human-readable task name."""

    prompt: str
    """The prompt to send to the agent when the task runs."""

    schedule: ScheduleInterval | CronExpression
    """When to run the task."""

    status: ScheduleStatus = ScheduleStatus.ACTIVE
    """Current task status."""

    working_dir: str | None = None
    """Working directory for the task."""

    model: str | None = None
    """Optional model override for this task."""

    max_turns: int = 50
    """Maximum agent turns per run."""

    created_at: float = field(default_factory=time.time)
    """When the task was created."""

    last_run_at: float | None = None
    """When the task last ran."""

    last_result: str | None = None
    """Summary of the last run result."""

    run_count: int = 0
    """Number of times the task has run."""

    failure_count: int = 0
    """Number of failed runs."""

    tags: list[str] = field(default_factory=list)
    """Tags for filtering/organizing tasks."""

    def is_due(self, now: float | None = None) -> bool:
        """Check if the task is due to run.

        Args:
            now: Current timestamp. Uses time.time() if None.

        Returns:
            True if the task should run now.
        """
        if self.status != ScheduleStatus.ACTIVE:
            return False

        current_time = now or time.time()

        if isinstance(self.schedule, CronExpression):
            return self.schedule.matches_time(current_time)

        if isinstance(self.schedule, ScheduleInterval):
            if self.last_run_at is None:
                return True
            return (current_time - self.last_run_at) >= self.schedule.seconds

        return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        schedule_dict: dict[str, Any]
        if isinstance(self.schedule, ScheduleInterval):
            schedule_dict = {
                "type": "interval",
                "value": self.schedule.value,
                "unit": self.schedule.unit,
            }
        else:
            schedule_dict = {
                "type": "cron",
                "expression": str(self.schedule),
            }

        return {
            "task_id": self.task_id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule": schedule_dict,
            "status": self.status,
            "working_dir": self.working_dir,
            "model": self.model,
            "max_turns": self.max_turns,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledTask:
        """Deserialize from dictionary.

        Args:
            data: Serialized task data.

        Returns:
            ScheduledTask instance.
        """
        schedule_data = data["schedule"]
        schedule: ScheduleInterval | CronExpression
        if schedule_data["type"] == "interval":
            schedule = ScheduleInterval(
                value=schedule_data["value"],
                unit=IntervalUnit(schedule_data["unit"]),
            )
        else:
            schedule = CronExpression.parse(schedule_data["expression"])

        return cls(
            task_id=data["task_id"],
            name=data["name"],
            prompt=data["prompt"],
            schedule=schedule,
            status=ScheduleStatus(data.get("status", "active")),
            working_dir=data.get("working_dir"),
            model=data.get("model"),
            max_turns=data.get("max_turns", 50),
            created_at=data.get("created_at", time.time()),
            last_run_at=data.get("last_run_at"),
            last_result=data.get("last_result"),
            run_count=data.get("run_count", 0),
            failure_count=data.get("failure_count", 0),
            tags=data.get("tags", []),
        )


def parse_schedule_string(schedule_str: str) -> ScheduleInterval | CronExpression:
    """Parse a human-friendly schedule string.

    Supports:
        - "every 5 minutes"
        - "every 2 hours"
        - "every day"
        - "0 9 * * 1-5"  (cron: weekdays at 9am)

    Args:
        schedule_str: Schedule specification.

    Returns:
        Parsed schedule.

    Raises:
        ValueError: If the string can't be parsed.
    """
    s = schedule_str.strip().lower()

    # Try interval format: "every N unit"
    match = re.match(r"every\s+(\d+)\s+(minute|hour|day)s?", s)
    if match:
        value = int(match.group(1))
        unit_str = match.group(2)
        unit_map = {"minute": IntervalUnit.MINUTES, "hour": IntervalUnit.HOURS, "day": IntervalUnit.DAYS}
        return ScheduleInterval(value=value, unit=unit_map[unit_str])

    # Try shorthand: "every day", "every hour"
    if s in ("every day", "daily"):
        return ScheduleInterval(value=1, unit=IntervalUnit.DAYS)
    if s in ("every hour", "hourly"):
        return ScheduleInterval(value=1, unit=IntervalUnit.HOURS)

    # Try cron expression
    parts = s.split()
    if len(parts) == 5:
        return CronExpression.parse(s)

    raise ValueError(f"Cannot parse schedule: {schedule_str}")


class ScheduledRunsStore:
    """Persistence layer for scheduled tasks."""

    def __init__(self, store_path: str | None = None) -> None:
        """Initialize the store.

        Args:
            store_path: Path to the JSON file for persistence.
        """
        if store_path is None:
            store_path = os.path.expanduser("~/.bog-agents/scheduled_tasks.json")
        self.store_path = Path(store_path)
        self._tasks: dict[str, ScheduledTask] = {}
        self._load()

    def _load(self) -> None:
        """Load tasks from disk."""
        if not self.store_path.exists():
            return
        content = self.store_path.read_text(encoding="utf-8").strip()
        if not content:
            return
        try:
            data = json.loads(content)
            for task_data in data.get("tasks", []):
                task = ScheduledTask.from_dict(task_data)
                self._tasks[task.task_id] = task
            logger.info("Loaded %d scheduled tasks", len(self._tasks))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Failed to load scheduled tasks: %s", exc)

    def save(self) -> None:
        """Persist tasks to disk."""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"tasks": [t.to_dict() for t in self._tasks.values()]}
        self.store_path.write_text(json.dumps(data, indent=2))

    def add(self, task: ScheduledTask) -> None:
        """Add a scheduled task.

        Args:
            task: The task to schedule.
        """
        self._tasks[task.task_id] = task
        self.save()

    def remove(self, task_id: str) -> bool:
        """Remove a scheduled task.

        Args:
            task_id: ID of the task to remove.

        Returns:
            True if found and removed.
        """
        if task_id in self._tasks:
            del self._tasks[task_id]
            self.save()
            return True
        return False

    def get_due_tasks(self) -> list[ScheduledTask]:
        """Get all tasks that are due to run now.

        Returns:
            List of tasks ready to execute.
        """
        return [t for t in self._tasks.values() if t.is_due()]

    def list_tasks(self, *, tag: str | None = None) -> list[ScheduledTask]:
        """List all scheduled tasks.

        Args:
            tag: Optional tag filter.

        Returns:
            Filtered task list.
        """
        tasks = list(self._tasks.values())
        if tag:
            tasks = [t for t in tasks if tag in t.tags]
        return sorted(tasks, key=lambda t: t.created_at)


class ScheduledRunsMiddleware(AgentMiddleware):
    """Middleware for cron-like scheduled agent runs.

    Example:
        ```python
        from bog_agents.middleware.scheduled_runs import (
            ScheduledRunsMiddleware,
            ScheduledTask,
            parse_schedule_string,
        )

        middleware = ScheduledRunsMiddleware()

        # Add a scheduled task
        middleware.schedule_task(
            name="dependency-check",
            prompt="Check for outdated dependencies and open PRs for updates",
            schedule="every day",
            working_dir="/path/to/project",
        )
        ```
    """

    store: ScheduledRunsStore

    def __init__(
        self,
        *,
        store_path: str | None = None,
    ) -> None:
        """Initialize scheduled runs middleware.

        Args:
            store_path: Path for task persistence.
        """
        self.store = ScheduledRunsStore(store_path)

    def schedule_task(
        self,
        *,
        name: str,
        prompt: str,
        schedule: str,
        working_dir: str | None = None,
        model: str | None = None,
        max_turns: int = 50,
        tags: list[str] | None = None,
    ) -> ScheduledTask:
        """Schedule a new recurring agent task.

        Args:
            name: Human-readable task name.
            prompt: Agent prompt to execute.
            schedule: Schedule string (e.g., "every 2 hours", "0 9 * * 1-5").
            working_dir: Working directory for execution.
            model: Optional model override.
            max_turns: Maximum agent turns per run.
            tags: Optional tags.

        Returns:
            The created ScheduledTask.
        """
        import uuid

        parsed_schedule = parse_schedule_string(schedule)
        task = ScheduledTask(
            task_id=str(uuid.uuid4())[:8],
            name=name,
            prompt=prompt,
            schedule=parsed_schedule,
            working_dir=working_dir,
            model=model,
            max_turns=max_turns,
            tags=tags or [],
        )
        self.store.add(task)
        logger.info("Scheduled task '%s' (%s): %s", name, task.task_id, parsed_schedule)
        return task

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a scheduled task.

        Args:
            task_id: Task ID to cancel.

        Returns:
            True if found and cancelled.
        """
        return self.store.remove(task_id)

    def get_due_tasks(self) -> list[ScheduledTask]:
        """Get tasks that are due to run now.

        Returns:
            List of due tasks.
        """
        return self.store.get_due_tasks()

    def list_tasks(self) -> list[ScheduledTask]:
        """List all scheduled tasks.

        Returns:
            All scheduled tasks.
        """
        return self.store.list_tasks()

    def mark_completed(self, task_id: str, result: str) -> None:
        """Mark a task run as completed.

        Args:
            task_id: Task that ran.
            result: Summary of the result.
        """
        tasks = self.store.list_tasks()
        for task in tasks:
            if task.task_id == task_id:
                task.last_run_at = time.time()
                task.last_result = result
                task.run_count += 1
                task.status = ScheduleStatus.ACTIVE
                self.store.save()
                return

    def mark_failed(self, task_id: str, error: str) -> None:
        """Mark a task run as failed.

        Args:
            task_id: Task that failed.
            error: Error description.
        """
        tasks = self.store.list_tasks()
        for task in tasks:
            if task.task_id == task_id:
                task.last_run_at = time.time()
                task.last_result = f"FAILED: {error}"
                task.failure_count += 1
                task.status = ScheduleStatus.ACTIVE
                self.store.save()
                return

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Pass through — scheduling is managed externally."""
        return await call_next(request, runtime)
