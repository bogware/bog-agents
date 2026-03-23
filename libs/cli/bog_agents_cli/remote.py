"""Remote/cloud execution mode for running agents on remote infrastructure.

Feature #21: Remote/cloud execution — push agent tasks to cloud instances.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RemoteStatus(StrEnum):
    """Status of a remote agent task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RemoteTask:
    """A task running on remote infrastructure."""

    task_id: str
    """Unique task identifier."""

    prompt: str
    """The task prompt/instructions."""

    status: RemoteStatus = RemoteStatus.PENDING
    """Current task status."""

    model: str = ""
    """Model being used."""

    output: str = ""
    """Task output when completed."""

    files_changed: list[str] = field(default_factory=list)
    """Files modified by the remote agent."""

    error: str = ""
    """Error message if failed."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""


@dataclass
class RemoteConfig:
    """Configuration for remote execution."""

    provider: str = "langgraph-cloud"
    """Remote execution provider."""

    api_url: str = ""
    """API endpoint URL."""

    api_key: str = ""
    """API authentication key."""

    workspace_sync: bool = True
    """Whether to sync local workspace to remote."""

    auto_apply: bool = False
    """Whether to auto-apply remote changes locally."""


def load_remote_config(config_dir: Path) -> RemoteConfig:
    """Load remote execution configuration.

    Args:
        config_dir: Config directory path.

    Returns:
        RemoteConfig instance.
    """
    config_file = config_dir / "remote.json"
    if not config_file.exists():
        return RemoteConfig()

    try:
        data = json.loads(config_file.read_text())
        return RemoteConfig(
            provider=data.get("provider", "langgraph-cloud"),
            api_url=data.get("api_url", ""),
            api_key=data.get("api_key", ""),
            workspace_sync=data.get("workspace_sync", True),
            auto_apply=data.get("auto_apply", False),
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load remote config: %s", e)
        return RemoteConfig()


async def submit_remote_task(
    config: RemoteConfig,
    prompt: str,
    *,
    model: str = "",
    working_dir: Path | None = None,  # noqa: ARG001  # Reserved for future workspace sync
) -> RemoteTask:
    """Submit a task for remote execution.

    Args:
        config: Remote execution configuration.
        prompt: Task instructions.
        model: Model to use.
        working_dir: Local workspace to sync.

    Returns:
        RemoteTask with initial status.
    """
    import uuid

    task_id = str(uuid.uuid4())[:8]

    if config.provider == "langgraph-cloud" and config.api_url:
        # Use LangGraph Cloud API
        try:
            from langgraph_sdk import get_client

            client = get_client(url=config.api_url)
            thread = await client.threads.create()
            run = await client.runs.create(
                thread["thread_id"],
                "agent",
                input={"messages": [{"role": "user", "content": prompt}]},
            )
            return RemoteTask(
                task_id=run["run_id"],
                prompt=prompt,
                status=RemoteStatus.RUNNING,
                model=model,
                metadata={"thread_id": thread["thread_id"]},
            )
        except Exception as e:
            return RemoteTask(
                task_id=task_id,
                prompt=prompt,
                status=RemoteStatus.FAILED,
                error=f"Failed to submit to LangGraph Cloud: {e}",
            )

    return RemoteTask(
        task_id=task_id,
        prompt=prompt,
        status=RemoteStatus.FAILED,
        error=f"Remote provider '{config.provider}' not configured. Set api_url in ~/.bog-agents/remote.json",
    )


async def check_remote_task(config: RemoteConfig, task: RemoteTask) -> RemoteTask:
    """Check the status of a remote task.

    Args:
        config: Remote execution configuration.
        task: The task to check.

    Returns:
        Updated RemoteTask.
    """
    if config.provider == "langgraph-cloud" and config.api_url:
        try:
            from langgraph_sdk import get_client

            client = get_client(url=config.api_url)
            thread_id = task.metadata.get("thread_id", "")
            run = await client.runs.get(thread_id, task.task_id)

            status_map = {
                "pending": RemoteStatus.PENDING,
                "running": RemoteStatus.RUNNING,
                "success": RemoteStatus.COMPLETED,
                "error": RemoteStatus.FAILED,
            }
            task.status = status_map.get(run.get("status", ""), RemoteStatus.PENDING)

            if task.status == RemoteStatus.COMPLETED:
                # Get the final state
                state = await client.threads.get_state(thread_id)
                messages = state.get("values", {}).get("messages", [])
                if messages:
                    last = messages[-1]
                    task.output = str(last.get("content", ""))
        except Exception as e:
            logger.warning("Failed to check remote task: %s", e)

    return task


def format_remote_tasks(tasks: list[RemoteTask]) -> str:
    """Format remote tasks for display.

    Args:
        tasks: List of remote tasks.

    Returns:
        Formatted string.
    """
    if not tasks:
        return "No remote tasks."

    lines = ["## Remote Tasks\n"]
    for task in tasks:
        status_icon = {
            RemoteStatus.PENDING: "...",
            RemoteStatus.RUNNING: ">>>",
            RemoteStatus.COMPLETED: " OK",
            RemoteStatus.FAILED: "ERR",
            RemoteStatus.CANCELLED: "---",
        }.get(task.status, "???")

        prompt_preview = (
            task.prompt[:60] + "..." if len(task.prompt) > 60 else task.prompt
        )
        lines.append(f"[{status_icon}] {task.task_id}: {prompt_preview}")

        if task.error:
            lines.append(f"  Error: {task.error}")
        if task.output:
            output_preview = (
                task.output[:100] + "..." if len(task.output) > 100 else task.output
            )
            lines.append(f"  Output: {output_preview}")

    return "\n".join(lines)
