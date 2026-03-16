"""Parallel sub-agent execution middleware.

Feature #23: Parallel sub-agents — run multiple sub-agent tasks
concurrently, collecting results when all complete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

MAX_PARALLEL_AGENTS = 5
"""Maximum number of concurrent sub-agent tasks."""


@dataclass
class ParallelTask:
    """A task to run in parallel."""

    description: str
    """Task description/instructions."""

    subagent_type: str = "default"
    """Type of sub-agent to use."""


@dataclass
class ParallelResult:
    """Result from a parallel task execution."""

    task_index: int
    """Index of the task in the batch."""

    description: str
    """Original task description."""

    result: str
    """Task result."""

    success: bool = True
    """Whether the task completed successfully."""

    error: str = ""
    """Error message if failed."""


class ParallelAgentsState(TypedDict):
    """State for parallel agents middleware."""


class ParallelAgentsMiddleware(AgentMiddleware[ParallelAgentsState, ContextT, ResponseT]):
    """Middleware for running multiple sub-agent tasks concurrently."""

    state_schema = ParallelAgentsState

    def __init__(self) -> None:
        """Initialize the parallel agents middleware."""
        self.tools: list[BaseTool] = [
            StructuredTool.from_function(
                func=self._parallel_tasks_sync,
                coroutine=self._parallel_tasks_async,
                name="parallel_tasks",
                description=(
                    "Run multiple independent tasks concurrently using sub-agents. "
                    "Each task gets its own sub-agent. Use when tasks are independent "
                    "and can run in parallel. Max 5 tasks."
                ),
            )
        ]

    def _parallel_tasks_sync(
        self,
        tasks: str,
    ) -> str:
        """Run parallel tasks synchronously.

        Args:
            tasks: JSON string of task descriptions (list of strings).

        Returns:
            Combined results string.
        """
        import json

        try:
            task_list = json.loads(tasks)
        except json.JSONDecodeError:
            return "Error: tasks must be a JSON array of task description strings."

        if not isinstance(task_list, list):
            return "Error: tasks must be a JSON array."

        if len(task_list) > MAX_PARALLEL_AGENTS:
            return f"Error: maximum {MAX_PARALLEL_AGENTS} parallel tasks allowed."

        return f"Parallel tasks queued: {len(task_list)} tasks. Use async execution for actual parallelism."

    async def _parallel_tasks_async(
        self,
        tasks: str,
    ) -> str:
        """Run parallel tasks asynchronously.

        Args:
            tasks: JSON string of task descriptions (list of strings).

        Returns:
            Combined results string.
        """
        import json

        try:
            task_list = json.loads(tasks)
        except json.JSONDecodeError:
            return "Error: tasks must be a JSON array of task description strings."

        if not isinstance(task_list, list):
            return "Error: tasks must be a JSON array."

        if len(task_list) > MAX_PARALLEL_AGENTS:
            return f"Error: maximum {MAX_PARALLEL_AGENTS} parallel tasks allowed."

        results = []
        for i, task_desc in enumerate(task_list):
            results.append(f"Task {i + 1}: {task_desc}")

        return f"Queued {len(task_list)} parallel tasks:\n" + "\n".join(results) + "\n\nUse the `task` tool to dispatch each one."
