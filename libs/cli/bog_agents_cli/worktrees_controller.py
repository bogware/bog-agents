"""TUI-free logic behind `/worktrees` (v6 CLI-1).

`/worktrees` fronts the managed background tasks that `/agent spawn --worktree`
creates. The parsing and rendering live here so the App handler stays a thin
dispatcher (the app.py size ratchet) and the behaviour is unit-testable
without Textual.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

USAGE = (
    "Usage: /worktrees | /worktrees status | /worktrees spawn <prompt> | "
    '/worktrees spawn [{"label":"...", "prompt":"..."}] | '
    "/worktrees merge <id|branch> | /worktrees cancel <id>"
)

NO_TASKS = "No worktree tasks.\n\nUse /worktrees spawn <prompt> (or a JSON array of {label, prompt}) to start some."


class _WorktreeTask(Protocol):
    status: str
    worktree_branch: str | None

    def status_line(self) -> str: ...


def parse_spawn_payload(payload: str) -> tuple[list[dict[str, str]], str | None]:
    """Turn the text after `spawn` into `{label, prompt}` items.

    Accepts either a plain prompt or the historical JSON array form.

    Args:
        payload: Everything after `/worktrees spawn`.

    Returns:
        `(items, error)` — `items` is empty when `error` is set.
    """
    payload = payload.strip()
    if payload.startswith("["):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return [], f"Invalid JSON: {exc}\n\n{USAGE}"
        if not isinstance(parsed, list):
            return [], "Input must be a JSON array of task objects."
        items = [
            {"label": str(item.get("label", "")), "prompt": str(item.get("prompt", ""))}
            for item in parsed
            if isinstance(item, dict)
        ]
    elif payload:
        items = [{"label": "", "prompt": payload}]
    else:
        items = []
    items = [item for item in items if item["prompt"].strip()]
    if not items:
        return [], USAGE
    return items, None


def agent_spawn_command(item: dict[str, str]) -> str:
    """Map one spawn item onto the `/agent spawn --worktree …` command it delegates to."""
    label_flag = f"--label {shlex.quote(item['label'])} " if item.get("label") else ""
    return f"/agent spawn --worktree {label_flag}{item['prompt']}"


def render_worktree_tasks(tasks: Iterable[_WorktreeTask]) -> str:
    """Render the worktree-backed tasks, or the honest empty message."""
    rows = [
        f"  {task.status_line()}  [dim]{task.worktree_branch}[/dim]"
        for task in tasks
        if task.worktree_branch
    ]
    if not rows:
        return NO_TASKS
    return "\n".join(["[bold]Worktree tasks[/bold]", *rows])


__all__ = [
    "NO_TASKS",
    "USAGE",
    "agent_spawn_command",
    "parse_spawn_payload",
    "render_worktree_tasks",
]


async def create_worktree_report(repo_root: Path, branch: str) -> tuple[str, bool]:
    """`/worktree create <branch>`: create it, apply `[worktree] reuse`, return `(message, ok)` (ROADMAP #76)."""
    if not branch:
        return "Usage: /worktree create <branch>", False
    from bog_agents.middleware.worktree import create_worktree

    try:
        info = await asyncio.to_thread(create_worktree, repo_root, branch)
    except (ValueError, OSError) as exc:
        return f"Invalid branch name: {exc}", False
    from bog_agents_cli.envcache import configured_reuse, reuse_into_worktree

    notes = await asyncio.to_thread(
        reuse_into_worktree, repo_root, info.path, configured_reuse()
    )
    text = f"Created worktree on branch {info.branch}\nPath: {info.path}"
    if notes:
        text += "\nEnvironment reuse:\n" + "\n".join(f"  {n}" for n in notes)
    return text, True
