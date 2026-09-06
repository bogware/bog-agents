"""`/subtask` and `/fork`: background forks of this conversation (ROADMAP #71).

Both hand work to `BackgroundAgentManager` so the TUI stays free. `/subtask
<prompt>` runs the prompt in the background with a brief of this conversation
in front of it (the fork half of "fork subagents" from the keyboard; the SDK
half is `mode: fork` on a `SubAgent`). `/fork [--worktree] [name]` records a
session fork (`session_fork.create_fork`) and starts a background agent that
continues the work — on a fresh worktree when asked — so the interactive
thread stays where it is. The logic is here, not on the App, so it tests
against a fake.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRIEF_MESSAGES = 12
BRIEF_CHARS = 6_000
USAGE_SUBTASK = "Usage: /subtask <prompt> — run a prompt in the background with this conversation as context"
USAGE_FORK = "Usage: /fork [--worktree] [name] — record a fork and continue the work in a background agent"


def conversation_brief(
    app: Any,  # noqa: ANN401 - the App
    *,
    limit: int = BRIEF_MESSAGES,
    chars: int = BRIEF_CHARS,
) -> str:
    """The last user / assistant turns as plain text, newest last, bounded."""
    store = getattr(app, "_message_store", None)
    if store is None:
        return ""
    try:
        messages = list(store.get_all_messages())
    except Exception:
        return ""
    lines: list[str] = []
    for message in messages:
        kind = str(
            getattr(getattr(message, "type", ""), "value", getattr(message, "type", ""))
        ).lower()
        content = str(getattr(message, "content", "") or "").strip()
        if kind not in ("user", "assistant") or not content:
            continue
        lines.append(f"{'User' if kind == 'user' else 'Assistant'}: {content}")
    text = "\n".join(lines[-limit:])
    return text[-chars:] if len(text) > chars else text


def forks_dir() -> Path:
    """Where `session_fork` records forks (`~/.bog-agents`)."""
    from bog_agents_cli.config import settings

    return Path(settings.user_agents_dir)


def fork_prompt(brief: str, instruction: str) -> str:
    """What the background agent is told: the brief, then the instruction."""
    if not brief:
        return instruction
    return f"You are continuing a conversation. Here is the recent context:\n\n{brief}\n\n---\n\n{instruction}"


def _parse_fork_args(rest: str) -> tuple[bool, str]:
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    worktree = "--worktree" in tokens
    name = " ".join(t for t in tokens if t != "--worktree").strip()
    return worktree, name


async def run_fork_command(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Body of `/subtask` and `/fork`."""
    from bog_agents_cli.widgets.messages import AppMessage

    text = command.strip()
    head, _, rest = text.partition(" ")
    rest = rest.strip()
    manager = getattr(app, "_bg_manager", None)
    if manager is None:
        await app._mount_message(
            AppMessage("Background agents are not available in this session.")
        )
        return
    brief = conversation_brief(app)
    if head.lower() == "/subtask":
        if not rest:
            await app._mount_message(AppMessage(USAGE_SUBTASK))
            return
        task_id = await manager.submit(
            fork_prompt(brief, rest),
            label="subtask",
            working_dir=str(getattr(app, "_cwd", "") or "") or None,
            parent_thread_id=getattr(app, "_lc_thread_id", None),
            metadata={"kind": "subtask", "original_prompt": rest},
        )
        await app._mount_message(
            AppMessage(
                f"Subtask {task_id} started in the background with this conversation as context; /tasks shows it, /background {task_id} reads the result."
            )
        )
        return
    worktree, name = _parse_fork_args(rest)
    from bog_agents_cli.session_fork import create_fork

    parent_thread = str(getattr(app, "_lc_thread_id", None) or "")
    fork = create_fork(
        forks_dir(),
        parent_thread or "session",
        name=name,
        message_count=len(brief.splitlines()),
    )
    instruction = f"Continue the work described above as a fork named {fork.name!r}. Finish it end to end and report what you changed."
    if name:
        instruction += f" Focus: {name}."
    prompt = fork_prompt(brief, instruction)
    if worktree and hasattr(app, "_handle_agent_command"):
        await app._handle_agent_command(
            f"/agent spawn --worktree --label fork-{fork.fork_id} {shlex.quote(prompt)}",
            echo=False,
        )
        where = "on a fresh worktree"
    else:
        await manager.submit(
            prompt,
            label=f"fork-{fork.fork_id}",
            working_dir=str(getattr(app, "_cwd", "") or "") or None,
            parent_thread_id=parent_thread or None,
            metadata={
                "kind": "fork",
                "fork_id": fork.fork_id,
                "fork_thread_id": fork.fork_thread_id,
            },
        )
        where = "in this working directory"
    await app._mount_message(
        AppMessage(
            f"Fork {fork.fork_id} ({fork.name}) is running {where} as a background agent; this thread stays put. /tasks shows it."
        )
    )


__all__ = [
    "USAGE_FORK",
    "USAGE_SUBTASK",
    "conversation_brief",
    "fork_prompt",
    "forks_dir",
    "run_fork_command",
]
