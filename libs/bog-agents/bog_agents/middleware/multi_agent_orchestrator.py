"""Middleware providing multi-agent orchestration with thread management.
Feature #2: Multi-agent orchestrator — spawn and manage multiple agent threads.
Feature #3: Agent thread management — list, switch, steer, stop threads.
Feature #4: CSV batch processing — one agent per CSV row.
Feature #5: Monitor agent role — long-polling status checker.
Feature #6: Cross-agent communication.

⚠ **STUB — NOT FOR PRODUCTION USE.**

This middleware is a scaffold that demonstrates the shape of a real
implementation. Its tools accept calls and return placeholder structures
so an agent can be wired against the surface, but the underlying logic
is not implemented — for example, ``fetch_quote`` returns ``price=0.0``
with a note instructing the caller to populate real data. Models that
call these tools will receive plausible-looking but **incorrect**
results.

This module ships at "Development Status :: 4 - Beta" deliberately;
see REVIEW.md P0-A for the broader plan (extract to a separate
``bog-agents-finance``-style package once the implementations are real,
or remove from the headline middleware list if they will not be).
Do not enable in any flow whose output is consumed by a downstream
system, customer-facing surface, or compliance-relevant artifact.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class AgentThread:
    """A managed agent thread."""

    thread_id: str
    label: str
    status: str = "pending"  # pending, running, completed, failed, stopped
    task: str = ""
    result: str = ""
    created_at: float = field(default_factory=time.time)
    messages: list[dict[str, str]] = field(default_factory=list)


class OrchestratorState(TypedDict):
    """State for the orchestrator middleware."""


class MultiAgentOrchestratorMiddleware(AgentMiddleware[OrchestratorState, ContextT, ResponseT]):
    """Middleware for orchestrating multiple parallel agent threads.

    Provides tools for spawning, monitoring, and communicating between
    multiple concurrent agent sessions.

    Args:
        max_threads: Maximum number of concurrent agent threads.
    """

    state_schema = OrchestratorState

    def __init__(self, *, max_threads: int = 10) -> None:
        self._max_threads = max_threads
        self._threads: dict[str, AgentThread] = {}
        self._active_thread: str | None = None
        self.tools = self._build_tools()

    @property
    def threads(self) -> dict[str, AgentThread]:
        """Access tracked threads."""
        return self._threads

    def _build_tools(self) -> list[BaseTool]:
        """Build orchestrator tools."""
        middleware = self

        def spawn_agent_thread(
            runtime: ToolRuntime[None, OrchestratorState],
            task: Annotated[str, "Task description for the new agent"],
            label: Annotated[str, "Human-readable label for this thread"] = "",
            use_worktree: Annotated[bool, "Whether to create a git worktree for isolation"] = False,
        ) -> str:
            """Spawn a new agent thread for parallel task execution."""
            if len(middleware._threads) >= middleware._max_threads:
                return f"Error: Maximum of {middleware._max_threads} concurrent threads reached."

            thread_id = str(uuid.uuid4())[:8]
            thread = AgentThread(
                thread_id=thread_id,
                label=label or f"thread-{thread_id}",
                task=task,
                status="pending",
            )
            middleware._threads[thread_id] = thread
            return f"Spawned thread '{thread.label}' (id={thread_id}) with task: {task}"

        def list_agent_threads(
            runtime: ToolRuntime[None, OrchestratorState],
        ) -> str:
            """List all agent threads with their status."""
            if not middleware._threads:
                return "No active agent threads."
            lines = []
            for tid, t in middleware._threads.items():
                marker = " *" if tid == middleware._active_thread else ""
                lines.append(f"  [{t.status}] {t.label} ({tid}){marker}: {t.task[:80]}")
            return "Agent threads:\n" + "\n".join(lines)

        def switch_thread(
            runtime: ToolRuntime[None, OrchestratorState],
            thread_id: Annotated[str, "Thread ID to switch to"],
        ) -> str:
            """Switch to a different agent thread."""
            if thread_id not in middleware._threads:
                return f"Thread '{thread_id}' not found."
            middleware._active_thread = thread_id
            thread = middleware._threads[thread_id]
            return f"Switched to thread '{thread.label}' ({thread_id}). Status: {thread.status}"

        def stop_thread(
            runtime: ToolRuntime[None, OrchestratorState],
            thread_id: Annotated[str, "Thread ID to stop"],
        ) -> str:
            """Stop a running agent thread."""
            if thread_id not in middleware._threads:
                return f"Thread '{thread_id}' not found."
            thread = middleware._threads[thread_id]
            thread.status = "stopped"
            return f"Stopped thread '{thread.label}' ({thread_id})"

        def close_thread(
            runtime: ToolRuntime[None, OrchestratorState],
            thread_id: Annotated[str, "Thread ID to close and remove"],
        ) -> str:
            """Close and remove a completed or stopped thread."""
            thread = middleware._threads.pop(thread_id, None)
            if thread is None:
                return f"Thread '{thread_id}' not found."
            if middleware._active_thread == thread_id:
                middleware._active_thread = None
            return f"Closed thread '{thread.label}' ({thread_id}). Final status: {thread.status}"

        def send_message_to_thread(
            runtime: ToolRuntime[None, OrchestratorState],
            thread_id: Annotated[str, "Target thread ID"],
            message: Annotated[str, "Message to send"],
        ) -> str:
            """Send a message to another agent thread for coordination."""
            if thread_id not in middleware._threads:
                return f"Thread '{thread_id}' not found."
            thread = middleware._threads[thread_id]
            thread.messages.append({"from": middleware._active_thread or "main", "content": message})
            return f"Message sent to thread '{thread.label}'"

        def read_thread_messages(
            runtime: ToolRuntime[None, OrchestratorState],
            thread_id: Annotated[str, "Thread ID to read messages from"] = "",
        ) -> str:
            """Read messages from an agent thread."""
            tid = thread_id or middleware._active_thread
            if not tid or tid not in middleware._threads:
                return "No thread selected or thread not found."
            thread = middleware._threads[tid]
            if not thread.messages:
                return f"No messages in thread '{thread.label}'"
            lines = [f"Messages in '{thread.label}':"]
            for msg in thread.messages:
                lines.append(f"  [{msg['from']}] {msg['content']}")
            return "\n".join(lines)

        def spawn_agents_on_csv(
            runtime: ToolRuntime[None, OrchestratorState],
            csv_content: Annotated[str, "CSV content with headers. Each row becomes one agent task."],
            task_template: Annotated[str, "Task template using {column_name} placeholders"],
        ) -> str:
            """Spawn one agent thread per CSV row for batch processing."""
            reader = csv.DictReader(io.StringIO(csv_content))
            spawned = 0
            for row in reader:
                if len(middleware._threads) >= middleware._max_threads:
                    return f"Spawned {spawned} threads. Hit max thread limit ({middleware._max_threads})."
                task = task_template.format(**row)
                thread_id = str(uuid.uuid4())[:8]
                thread = AgentThread(
                    thread_id=thread_id,
                    label=f"batch-{thread_id}",
                    task=task,
                    status="pending",
                )
                middleware._threads[thread_id] = thread
                spawned += 1
            return f"Spawned {spawned} agent threads from CSV data."

        def monitor_status(
            runtime: ToolRuntime[None, OrchestratorState],
            check_interval_seconds: Annotated[int, "How often to report status"] = 30,
        ) -> str:
            """Report status of all running agent threads. Use for monitoring workflows."""
            running = [t for t in middleware._threads.values() if t.status == "running"]
            completed = [t for t in middleware._threads.values() if t.status == "completed"]
            failed = [t for t in middleware._threads.values() if t.status == "failed"]
            pending = [t for t in middleware._threads.values() if t.status == "pending"]

            lines = [
                f"Thread Status Report ({len(middleware._threads)} total):",
                f"  Running: {len(running)}",
                f"  Completed: {len(completed)}",
                f"  Failed: {len(failed)}",
                f"  Pending: {len(pending)}",
            ]
            for t in running:
                lines.append(f"  [running] {t.label}: {t.task[:60]}")
            return "\n".join(lines)

        return [
            StructuredTool.from_function(name="spawn_agent_thread", description="Spawn a parallel agent thread.", func=spawn_agent_thread),
            StructuredTool.from_function(name="list_agent_threads", description="List all agent threads.", func=list_agent_threads),
            StructuredTool.from_function(name="switch_thread", description="Switch active thread.", func=switch_thread),
            StructuredTool.from_function(name="stop_thread", description="Stop a running thread.", func=stop_thread),
            StructuredTool.from_function(name="close_thread", description="Close and remove a thread.", func=close_thread),
            StructuredTool.from_function(name="send_message_to_thread", description="Send message to another thread.", func=send_message_to_thread),
            StructuredTool.from_function(name="read_thread_messages", description="Read messages from a thread.", func=read_thread_messages),
            StructuredTool.from_function(name="spawn_agents_on_csv", description="Spawn one agent per CSV row.", func=spawn_agents_on_csv),
            StructuredTool.from_function(name="monitor_status", description="Report thread status.", func=monitor_status),
        ]
