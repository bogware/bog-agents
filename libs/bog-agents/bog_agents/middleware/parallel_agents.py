"""Parallel sub-agent execution middleware.

Provides a ``parallel_tasks`` tool that runs multiple independent agent
tasks concurrently via ``asyncio.gather``, collecting results when all
complete.

Each task spawns an isolated agent instance through a caller-provided
factory and invokes it independently. This is complementary to the
``task`` tool in :mod:`~bog_agents.middleware.subagents` which supports
parallelism through LangGraph's native multi-tool-call mechanism.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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


async def _run_single_task(
    agent: Any,  # noqa: ANN401
    prompt: str,
    index: int,
) -> ParallelResult:
    """Run a single agent task and return a ``ParallelResult``.

    Args:
        agent: A compiled LangGraph agent (or anything with ``ainvoke``).
        prompt: The task prompt.
        index: Task index for identification.

    Returns:
        A ``ParallelResult`` with the outcome.
    """
    thread_id = f"parallel-{index}-{uuid.uuid4().hex[:8]}"
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    input_data: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        result = await agent.ainvoke(input_data, config=config)
        messages = result.get("messages", [])
        response = ""
        for msg in reversed(messages):
            if hasattr(msg, "content") and getattr(msg, "type", None) == "ai":
                response = msg.content
                break
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                response = msg.get("content", "")
                break

        return ParallelResult(
            task_index=index,
            description=prompt,
            result=response or "(no response)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Parallel task %d failed: %s", index, exc)
        return ParallelResult(
            task_index=index,
            description=prompt,
            result="",
            success=False,
            error=str(exc),
        )


class ParallelAgentsMiddleware(AgentMiddleware[ParallelAgentsState, ContextT, ResponseT]):
    """Middleware that exposes a ``parallel_tasks`` tool.

    The tool accepts a JSON array of task descriptions and runs each one
    concurrently in its own agent instance.

    Args:
        agent_factory: Callable that returns a new compiled agent each
            time it is called. Each parallel task gets its own agent to
            avoid shared-state issues. If ``None``, the tool returns an
            error asking the caller to configure one.
    """

    state_schema = ParallelAgentsState

    def __init__(
        self,
        *,
        agent_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Initialize the parallel agents middleware.

        Args:
            agent_factory: Creates a fresh agent per task. Pass ``None``
                to defer configuration (the tool will return a helpful
                error if invoked without one).
        """
        self._agent_factory = agent_factory
        self.tools: list[BaseTool] = [
            StructuredTool.from_function(
                func=self._parallel_tasks_sync,
                coroutine=self._parallel_tasks_async,
                name="parallel_tasks",
                description=(
                    "Run multiple independent tasks concurrently using sub-agents. "
                    "Each task gets its own agent instance. Use when tasks are independent "
                    "and can run in parallel (e.g. IAC, UI, API, Docs simultaneously). "
                    f"Max {MAX_PARALLEL_AGENTS} tasks. "
                    "Input: JSON array of task description strings."
                ),
            ),
        ]

    def _parallel_tasks_sync(self, tasks: str) -> str:
        """Synchronous fallback -- runs tasks sequentially.

        Args:
            tasks: JSON string of task descriptions (list of strings).

        Returns:
            Combined results string.
        """
        import json  # noqa: PLC0415

        try:
            task_list = json.loads(tasks)
        except json.JSONDecodeError:
            return "Error: tasks must be a JSON array of task description strings."

        if not isinstance(task_list, list):
            return "Error: tasks must be a JSON array."

        if len(task_list) > MAX_PARALLEL_AGENTS:
            return f"Error: maximum {MAX_PARALLEL_AGENTS} parallel tasks allowed."

        if self._agent_factory is None:
            return (
                "Error: parallel_tasks requires an agent_factory. "
                "Configure ParallelAgentsMiddleware(agent_factory=...) to use this tool."
            )

        return (
            f"Accepted {len(task_list)} tasks. "
            "Use the async execution path (agent.ainvoke) for true parallelism."
        )

    async def _parallel_tasks_async(self, tasks: str) -> str:
        """Run all tasks concurrently with ``asyncio.gather``.

        Args:
            tasks: JSON string of task descriptions (list of strings).

        Returns:
            Combined results string with each task's outcome.
        """
        import json  # noqa: PLC0415

        try:
            task_list = json.loads(tasks)
        except json.JSONDecodeError:
            return "Error: tasks must be a JSON array of task description strings."

        if not isinstance(task_list, list) or not all(isinstance(t, str) for t in task_list):
            return "Error: tasks must be a JSON array of strings."

        if len(task_list) > MAX_PARALLEL_AGENTS:
            return f"Error: maximum {MAX_PARALLEL_AGENTS} parallel tasks allowed, got {len(task_list)}."

        if not task_list:
            return "Error: at least one task is required."

        if self._agent_factory is None:
            return (
                "Error: parallel_tasks requires an agent_factory. "
                "Configure ParallelAgentsMiddleware(agent_factory=...) to use this tool."
            )

        logger.info("Starting %d parallel tasks", len(task_list))

        # Create one agent per task and run all concurrently
        coros = []
        for i, task_desc in enumerate(task_list):
            agent = self._agent_factory()
            coros.append(_run_single_task(agent, task_desc, i))

        results: list[ParallelResult] = await asyncio.gather(*coros)

        # Format results
        lines: list[str] = [f"Completed {len(results)} parallel tasks:\n"]
        for r in sorted(results, key=lambda x: x.task_index):
            status = "OK" if r.success else "FAILED"
            lines.append(f"--- Task {r.task_index + 1} [{status}]: {r.description[:60]} ---")
            if r.success:
                lines.append(r.result[:2000] if r.result else "(empty result)")
            else:
                lines.append(f"Error: {r.error}")
            lines.append("")

        succeeded = sum(1 for r in results if r.success)
        lines.append(f"\nSummary: {succeeded}/{len(results)} tasks succeeded.")

        return "\n".join(lines)
