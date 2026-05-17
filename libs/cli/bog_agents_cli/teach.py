"""Teach command for learning workflows from user actions.

Feature #45: /teach command — watch the user perform a task manually,
then learn and repeat it as a skill.
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
class RecordedAction:
    """A single recorded user action."""

    timestamp: float
    """When the action occurred."""

    action_type: str
    """Type of action (tool_call, message, command)."""

    tool_name: str = ""
    """Tool name if this was a tool call."""

    tool_args: dict[str, Any] = field(default_factory=dict)
    """Tool arguments."""

    result: str = ""
    """Result of the action."""

    message: str = ""
    """User message if this was a message."""


@dataclass
class TeachSession:
    """An active teaching session recording user actions."""

    name: str
    """Name for the skill being taught."""

    description: str = ""
    """Description of what the skill does."""

    actions: list[RecordedAction] = field(default_factory=list)
    """Recorded actions."""

    started_at: float = 0.0
    """Session start timestamp."""

    context: dict[str, Any] = field(default_factory=dict)
    """Context at session start (cwd, git branch, etc.)."""

    def __post_init__(self) -> None:
        """Set start time."""
        if self.started_at == 0:
            self.started_at = time.time()

    def record_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: str = "",
    ) -> None:
        """Record a tool call action.

        Args:
            tool_name: Name of the tool.
            tool_args: Arguments passed to the tool.
            result: Result from the tool.
        """
        # L1: strip credential-bearing keys before recording. Teach
        # recordings live on disk under ~/.bog-agents/teach/, so the
        # same denylist that protects replay sessions applies here.
        from bog_agents_cli.replay import _redact_secrets

        self.actions.append(
            RecordedAction(
                timestamp=time.time(),
                action_type="tool_call",
                tool_name=tool_name,
                tool_args=_redact_secrets(tool_args or {}),
                result=result[:500],  # Truncate long results
            )
        )

    def record_message(self, message: str) -> None:
        """Record a user message.

        Args:
            message: The user's message.
        """
        self.actions.append(
            RecordedAction(
                timestamp=time.time(),
                action_type="message",
                message=message,
            )
        )


def generate_skill_from_session(session: TeachSession) -> str:
    """Generate a skill file from a teaching session.

    Converts recorded actions into a reusable skill definition
    in markdown format with YAML frontmatter.

    Args:
        session: The completed teaching session.

    Returns:
        Skill file content as a string.
    """
    # Build the skill content
    lines = [
        "---",
        f"name: {session.name}",
        f"description: {session.description}",
        "---",
        "",
        f"# {session.name}",
        "",
        f"{session.description}",
        "",
        "## Steps",
        "",
    ]

    step_num = 0
    for action in session.actions:
        if action.action_type == "tool_call":
            step_num += 1
            lines.append(f"### Step {step_num}: {action.tool_name}")
            lines.append("")

            if action.tool_args:
                lines.append("**Arguments:**")
                for key, value in action.tool_args.items():
                    # Generalize paths
                    val_str = str(value)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    lines.append(f"- `{key}`: `{val_str}`")
                lines.append("")

            if action.result:
                lines.append("**Expected result pattern:**")
                result_preview = action.result[:300]
                lines.append(f"```\n{result_preview}\n```")
                lines.append("")

        elif action.action_type == "message" and action.message:
            lines.append(f"> User: {action.message}")
            lines.append("")

    lines.extend(
        [
            "",
            "## Usage",
            "",
            f"This skill was learned from a teaching session. "
            f"It recorded {len(session.actions)} actions over "
            f"{time.time() - session.started_at:.0f} seconds.",
            "",
            "To adapt this skill to a different context, the agent should:",
            "1. Understand the pattern of operations",
            "2. Adapt file paths and arguments to the current project",
            "3. Verify each step before proceeding",
        ]
    )

    return "\n".join(lines)


def save_taught_skill(
    skills_dir: Path,
    session: TeachSession,
) -> Path:
    """Save a taught skill to the skills directory.

    Args:
        skills_dir: Directory to save the skill file.
        session: The completed teaching session.

    Returns:
        Path to the saved skill file.
    """
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize name for filename
    safe_name = session.name.replace(" ", "-").lower()
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "-")
    skill_path = skills_dir / f"{safe_name}.md"

    content = generate_skill_from_session(session)
    skill_path.write_text(content)

    logger.info("Saved taught skill to %s", skill_path)
    return skill_path


def save_session_data(
    config_dir: Path,
    session: TeachSession,
) -> Path:
    """Save raw session data for replay.

    Args:
        config_dir: Config directory.
        session: The teaching session.

    Returns:
        Path to the saved session data file.
    """
    sessions_dir = config_dir / "teach_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "name": session.name,
        "description": session.description,
        "started_at": session.started_at,
        "context": session.context,
        "actions": [
            {
                "timestamp": a.timestamp,
                "action_type": a.action_type,
                "tool_name": a.tool_name,
                "tool_args": a.tool_args,
                "result": a.result,
                "message": a.message,
            }
            for a in session.actions
        ],
    }

    safe_name = session.name.replace(" ", "-").lower()
    session_path = sessions_dir / f"{safe_name}.json"
    session_path.write_text(json.dumps(data, indent=2))

    return session_path
