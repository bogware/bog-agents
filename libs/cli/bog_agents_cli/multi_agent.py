"""Multi-agent orchestration CLI interface.

Feature #1: Git worktree isolation.
Feature #2: Multi-agent orchestrator.
Feature #3: Agent thread management.
Feature #4: CSV batch processing.
Feature #5: Monitor agent role.
Feature #6: Cross-agent communication.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ThreadInfo:
    """Information about an agent thread."""

    thread_id: str
    label: str
    status: str = "pending"  # pending, running, completed, failed, stopped
    task: str = ""
    worktree_path: str = ""
    branch: str = ""
    created_at: float = field(default_factory=time.time)


def parse_agent_command(text: str) -> dict[str, str]:
    """Parse an /agent command.

    Supported subcommands:
    - /agent list — list threads
    - /agent spawn <task> — spawn a new thread
    - /agent switch <id> — switch to thread
    - /agent stop <id> — stop a thread
    - /agent close <id> — close and remove
    - /agent status — show all thread statuses

    Args:
        text: Command text after /agent.

    Returns:
        Parsed command dict with 'action' and optional params.
    """
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else "list"
    arg = parts[1] if len(parts) > 1 else ""

    return {"action": action, "argument": arg}


def format_thread_list(threads: list[ThreadInfo]) -> str:
    """Format thread list for display.

    Args:
        threads: List of thread info.

    Returns:
        Formatted string.
    """
    if not threads:
        return "No active agent threads."

    lines = ["Agent Threads:"]
    for t in threads:
        elapsed = time.time() - t.created_at
        mins = int(elapsed // 60)
        wt = f" (worktree: {t.branch})" if t.branch else ""
        lines.append(f"  [{t.status}] {t.label} ({t.thread_id}){wt} — {mins}m")
        if t.task:
            lines.append(f"           Task: {t.task[:80]}")
    return "\n".join(lines)


def format_thread_status(threads: list[ThreadInfo]) -> str:
    """Format thread status summary.

    Args:
        threads: All threads.

    Returns:
        Status summary string.
    """
    by_status: dict[str, int] = {}
    for t in threads:
        by_status[t.status] = by_status.get(t.status, 0) + 1

    total = len(threads)
    lines = [f"Thread Status ({total} total):"]
    for status, count in sorted(by_status.items()):
        lines.append(f"  {status}: {count}")
    return "\n".join(lines)


def generate_spawn_prompt(task: str, use_worktree: bool = False) -> str:
    """Generate a prompt for spawning a new agent thread.

    Args:
        task: Task description.
        use_worktree: Whether to use git worktree isolation.

    Returns:
        Prompt text for the agent.
    """
    prompt = f"Spawn a new agent thread for the following task:\n\n{task}\n"
    if use_worktree:
        prompt += "\nUse git worktree isolation for this thread."
    return prompt
