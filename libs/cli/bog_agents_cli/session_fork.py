"""Session forking — branch conversations into parallel paths.

Feature #22: Session forking — snapshot the current conversation state
and create a new branch to explore alternative approaches.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SessionFork:
    """A forked session branch."""

    fork_id: str
    """Unique identifier for this fork."""

    parent_thread_id: str
    """Thread ID this fork was created from."""

    fork_thread_id: str
    """New thread ID for the forked session."""

    name: str = ""
    """Human-readable name for the fork."""

    description: str = ""
    """Why this fork was created."""

    created_at: float = 0.0
    """Timestamp when the fork was created."""

    message_count: int = 0
    """Number of messages at fork point."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""


def create_fork(
    config_dir: Path,
    parent_thread_id: str,
    *,
    name: str = "",
    description: str = "",
    message_count: int = 0,
) -> SessionFork:
    """Create a new session fork.

    Args:
        config_dir: Config directory.
        parent_thread_id: Current thread ID.
        name: Name for the fork.
        description: Description of fork purpose.
        message_count: Current message count.

    Returns:
        SessionFork with new thread ID.
    """
    import uuid

    fork_id = str(uuid.uuid4())[:8]
    fork_thread_id = f"{parent_thread_id}-fork-{fork_id}"

    fork = SessionFork(
        fork_id=fork_id,
        parent_thread_id=parent_thread_id,
        fork_thread_id=fork_thread_id,
        name=name or f"Fork {fork_id}",
        description=description,
        created_at=time.time(),
        message_count=message_count,
    )

    _save_fork(config_dir, fork)
    return fork


def _save_fork(config_dir: Path, fork: SessionFork) -> None:
    """Save a fork record to disk.

    Args:
        config_dir: Config directory.
        fork: Fork to save.
    """
    forks_dir = config_dir / "forks"
    forks_dir.mkdir(parents=True, exist_ok=True)

    forks_file = forks_dir / f"{fork.parent_thread_id}.json"
    existing: list[dict[str, Any]] = []

    if forks_file.exists():
        try:
            existing = json.loads(forks_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.append(
        {
            "fork_id": fork.fork_id,
            "parent_thread_id": fork.parent_thread_id,
            "fork_thread_id": fork.fork_thread_id,
            "name": fork.name,
            "description": fork.description,
            "created_at": fork.created_at,
            "message_count": fork.message_count,
            "metadata": fork.metadata,
        }
    )

    forks_file.write_text(json.dumps(existing, indent=2))


def list_forks(config_dir: Path, thread_id: str) -> list[SessionFork]:
    """List all forks for a thread.

    Args:
        config_dir: Config directory.
        thread_id: Thread to list forks for.

    Returns:
        List of SessionFork instances.
    """
    forks_file = config_dir / "forks" / f"{thread_id}.json"
    if not forks_file.exists():
        return []

    try:
        data = json.loads(forks_file.read_text(encoding="utf-8"))
        return [
            SessionFork(
                fork_id=f["fork_id"],
                parent_thread_id=f["parent_thread_id"],
                fork_thread_id=f["fork_thread_id"],
                name=f.get("name", ""),
                description=f.get("description", ""),
                created_at=f.get("created_at", 0),
                message_count=f.get("message_count", 0),
                metadata=f.get("metadata", {}),
            )
            for f in data
        ]
    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.warning("Failed to load forks for %s: %s", thread_id, e)
        return []


def format_forks(forks: list[SessionFork]) -> str:
    """Format forks for display.

    Args:
        forks: List of forks.

    Returns:
        Formatted string.
    """
    if not forks:
        return "No forks for this session."

    lines = ["## Session Forks\n"]
    for fork in forks:
        lines.append(f"- **{fork.name}** ({fork.fork_id})")
        if fork.description:
            lines.append(f"  {fork.description}")
        lines.append(f"  Thread: {fork.fork_thread_id}")
        lines.append(f"  Messages at fork: {fork.message_count}")
    return "\n".join(lines)
