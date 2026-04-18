"""Middleware for synthesizing results from parallel agent tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from bog_agents.middleware.worktree import ParallelWorktreeMiddleware, WorktreeTask

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed"})


class ResultSynthesisState(TypedDict):
    """State for the result synthesis middleware (no extra state needed)."""


class ResultSynthesisMiddleware(AgentMiddleware[ResultSynthesisState, ContextT, ResponseT]):
    """Automate synthesis of results from parallel worktree tasks.

    When `ParallelWorktreeMiddleware` completes multiple parallel tasks,
    the main agent can use this middleware's tools to gather results and
    build a structured synthesis prompt, then reason over them without
    manual copy-paste.

    Args:
        parallel_middleware: If provided, watches this middleware's tasks
            automatically. Can also be linked later via the agent itself.
        synthesis_template: Optional Jinja2-free template string for the
            synthesis prompt. If ``None``, the default template is used.
    """

    state_schema = ResultSynthesisState

    def __init__(
        self,
        *,
        parallel_middleware: ParallelWorktreeMiddleware | None = None,
        synthesis_template: str | None = None,
    ) -> None:
        self._parallel_middleware = parallel_middleware
        self._synthesis_template = synthesis_template
        self._tools = self._build_tools()

    @property
    def tools(self) -> list[BaseTool]:
        """Expose result-synthesis tools to the agent."""
        return self._tools

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_tasks(self, task_ids: str) -> list[WorktreeTask]:
        """Return tasks matching the given IDs (or all tasks if ``'all'``).

        Args:
            task_ids: Comma-separated task IDs, or the literal string ``'all'``.

        Returns:
            Matching list of `WorktreeTask` objects.
        """
        if self._parallel_middleware is None:
            return []
        all_tasks = self._parallel_middleware.get_tasks()
        if task_ids.strip().lower() == "all":
            return all_tasks
        ids = {i.strip() for i in task_ids.split(",") if i.strip()}
        return [t for t in all_tasks if t.task_id in ids]

    @staticmethod
    def _format_results_table(tasks: list[WorktreeTask]) -> str:
        """Format tasks as a Rich-compatible table string.

        Columns: ID | Label | Status | Duration | Result Preview (200 chars).

        Args:
            tasks: List of `WorktreeTask` objects to format.

        Returns:
            Human-readable table string.
        """
        if not tasks:
            return "No tasks found."

        header = f"{'ID':<14} {'Label':<30} {'Status':<12} {'Duration':>10}  Result Preview"
        separator = "-" * 120
        rows = [header, separator]

        for task in tasks:
            dur = task.duration_secs
            dur_str = f"{dur:.1f}s" if dur is not None else "—"
            label = (task.label or task.task_id)[:29]
            preview_raw = (task.result or task.error or "")
            preview = preview_raw[:200].replace("\n", " ")
            rows.append(f"{task.task_id:<14} {label:<30} {task.status:<12} {dur_str:>10}  {preview}")

        return "\n".join(rows)

    # ------------------------------------------------------------------
    # Tool builders
    # ------------------------------------------------------------------

    def _build_tools(self) -> list[BaseTool]:
        """Build and return the synthesis tool set."""
        mw = self

        # ----- gather_parallel_results -----

        def gather_parallel_results(
            task_ids: Annotated[
                str,
                "Comma-separated task IDs to gather, or 'all' for all tasks",
            ],
        ) -> str:
            """Gather results from parallel worktree tasks.

            Polls the linked ParallelWorktreeMiddleware for tasks matching the
            supplied IDs and returns a formatted results table showing each
            task's label, status, duration, and full result text.
            """
            tasks = mw._resolve_tasks(task_ids)
            if not tasks:
                return (
                    "No tasks found. Ensure ParallelWorktreeMiddleware is linked "
                    "and tasks have been spawned."
                )
            table = mw._format_results_table(tasks)
            # Append full result text for completed tasks
            full_results: list[str] = [table, ""]
            for task in tasks:
                if task.result:
                    full_results.append(f"=== [{task.task_id}] {task.label} ===")
                    full_results.append(task.result)
                    full_results.append("")
            return "\n".join(full_results).rstrip()

        # ----- synthesize_parallel_results -----

        def synthesize_parallel_results(
            task_ids: Annotated[
                str,
                "Comma-separated task IDs to synthesize, or 'all' for all tasks",
            ],
            goal: Annotated[str, "The synthesis goal — what question or objective the combined results should answer"],
        ) -> str:
            """Build a structured synthesis prompt from parallel task results.

            Returns the prompt text for the agent to reason over — does NOT
            send a request to the model itself. The agent should use the
            returned prompt as input for its own reasoning step.
            """
            tasks = mw._resolve_tasks(task_ids)
            completed = [t for t in tasks if t.result]

            if not completed:
                return (
                    "No completed tasks with results found for the requested IDs. "
                    "Use `await_tasks_complete` first if tasks are still running."
                )

            if mw._synthesis_template is not None:
                # Simple template: replace {goal} and {results}
                results_block = "\n\n".join(
                    f"## Task: {t.label or t.task_id}\n{t.result}" for t in completed
                )
                return mw._synthesis_template.replace("{goal}", goal).replace("{results}", results_block)

            # Default structured prompt
            sections: list[str] = []

            sections.append("# Synthesis Goal")
            sections.append(goal)
            sections.append("")

            sections.append("# Individual Task Results")
            for task in completed:
                sections.append(f"## {task.label or task.task_id}")
                sections.append(task.result)
                sections.append("")

            sections.append("# Instructions")
            sections.append(
                "Synthesize the above results into a coherent response. "
                "Identify common themes, reconcile any conflicts, and produce "
                "a unified answer to the goal."
            )

            return "\n".join(sections)

        # ----- await_tasks_complete (async) -----

        async def await_tasks_complete(
            task_ids: Annotated[
                str,
                "Comma-separated task IDs to wait for, or 'all' for all tasks",
            ],
            timeout_seconds: Annotated[int, "Maximum seconds to wait before returning (default 300)"] = 300,
        ) -> str:
            """Wait until all requested tasks reach a terminal status.

            Polls task statuses every 2 seconds until all are completed or
            failed, or until the timeout is reached. Returns a summary of
            final statuses.
            """
            if mw._parallel_middleware is None:
                return "No ParallelWorktreeMiddleware linked — cannot poll task statuses."

            deadline = time.monotonic() + timeout_seconds
            while True:
                tasks = mw._resolve_tasks(task_ids)
                if not tasks:
                    return "No tasks found matching the requested IDs."

                pending = [t for t in tasks if t.status not in _TERMINAL_STATUSES]
                if not pending:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = ", ".join(t.task_id for t in pending)
                    return (
                        f"Timeout after {timeout_seconds}s. "
                        f"Still pending: {timed_out}\n"
                        + mw._format_results_table(tasks)
                    )

                await asyncio.sleep(2)

            tasks = mw._resolve_tasks(task_ids)
            return "All tasks complete.\n\n" + mw._format_results_table(tasks)

        async def _await_tasks_complete_sync_wrapper(
            task_ids: Annotated[str, "Comma-separated task IDs to wait for, or 'all' for all tasks"],
            timeout_seconds: Annotated[int, "Maximum seconds to wait before returning (default 300)"] = 300,
        ) -> str:
            return await await_tasks_complete(task_ids, timeout_seconds)

        # ----- register_parallel_middleware -----

        def register_parallel_middleware(
            middleware_ref: Annotated[str, "Reference identifier for the parallel middleware (informational)"],
        ) -> str:
            """Register a reference to a ParallelWorktreeMiddleware instance.

            Note: Actual linking is done via the `parallel_middleware` argument
            to `ResultSynthesisMiddleware.__init__`. This tool is a no-op
            placeholder for agent-initiated registration flows.
            """
            return "Middleware registered"

        # Synchronous fallback — used only when no event loop is running.
        def _sync_await(
            task_ids: str,
            timeout_seconds: int = 300,
        ) -> str:
            """Synchronous fallback for await_tasks_complete."""
            return asyncio.run(await_tasks_complete(task_ids, timeout_seconds))

        return [
            StructuredTool.from_function(
                name="gather_parallel_results",
                description=(
                    "Gather and display results from parallel worktree tasks. "
                    "Pass comma-separated task IDs or 'all' for all tasks."
                ),
                func=gather_parallel_results,
            ),
            StructuredTool.from_function(
                name="synthesize_parallel_results",
                description=(
                    "Build a structured LLM prompt that merges multiple parallel "
                    "task results toward a single goal. Returns the prompt text — "
                    "the agent should reason over it directly."
                ),
                func=synthesize_parallel_results,
            ),
            StructuredTool.from_function(
                name="await_tasks_complete",
                description=(
                    "Poll parallel worktree tasks every 2 s until all reach a "
                    "terminal status (completed/failed) or the timeout expires."
                ),
                func=_sync_await,
                coroutine=_await_tasks_complete_sync_wrapper,
            ),
            StructuredTool.from_function(
                name="register_parallel_middleware",
                description=(
                    "Register a reference to a ParallelWorktreeMiddleware. "
                    "Actual linking is done via __init__; this is a no-op placeholder."
                ),
                func=register_parallel_middleware,
            ),
        ]


__all__ = [
    "ResultSynthesisMiddleware",
    "ResultSynthesisState",
]
