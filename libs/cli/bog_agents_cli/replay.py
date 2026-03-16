"""Agent replay — record and replay agent sessions.

Feature #50: Agent replay — record an agent session and replay it on a
different codebase or branch, adapting actions to the new context.
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
class ReplayAction:
    """A recorded action in a replay session."""

    step: int
    """Step number in the sequence."""

    action_type: str
    """Type: 'tool_call', 'ai_message', 'user_message'."""

    tool_name: str = ""
    """Tool name for tool calls."""

    tool_args: dict[str, Any] = field(default_factory=dict)
    """Tool arguments (generalized)."""

    content: str = ""
    """Message content for ai/user messages."""

    result_pattern: str = ""
    """Expected result pattern for verification."""


@dataclass
class ReplaySession:
    """A complete recorded session that can be replayed."""

    session_id: str
    """Unique session identifier."""

    name: str = ""
    """Human-readable name."""

    description: str = ""
    """What the session accomplishes."""

    recorded_at: float = 0.0
    """When the session was recorded."""

    original_context: dict[str, Any] = field(default_factory=dict)
    """Original context (cwd, git info, etc.)."""

    actions: list[ReplayAction] = field(default_factory=list)
    """Ordered list of actions to replay."""

    variables: dict[str, str] = field(default_factory=dict)
    """Variables that should be adapted to the new context."""


class SessionRecorder:
    """Records agent actions for later replay."""

    def __init__(self, session_id: str, name: str = "") -> None:
        self._session = ReplaySession(
            session_id=session_id,
            name=name,
            recorded_at=time.time(),
        )
        self._step = 0
        self._recording = False

    def start_recording(self, context: dict[str, Any] | None = None) -> None:
        """Start recording actions.

        Args:
            context: Current context information.
        """
        self._recording = True
        self._session.original_context = context or {}
        logger.info("Started recording session %s", self._session.session_id)

    def stop_recording(self) -> ReplaySession:
        """Stop recording and return the session.

        Returns:
            The completed ReplaySession.
        """
        self._recording = False
        logger.info(
            "Stopped recording session %s (%d actions)",
            self._session.session_id,
            len(self._session.actions),
        )
        return self._session

    @property
    def is_recording(self) -> bool:
        """Whether recording is active."""
        return self._recording

    def record_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        result: str = "",
    ) -> None:
        """Record a tool call.

        Args:
            tool_name: Tool name.
            tool_args: Tool arguments.
            result: Tool result.
        """
        if not self._recording:
            return

        self._step += 1
        self._session.actions.append(
            ReplayAction(
                step=self._step,
                action_type="tool_call",
                tool_name=tool_name,
                tool_args=self._generalize_args(tool_args),
                result_pattern=result[:200] if result else "",
            )
        )

    def record_message(self, content: str, role: str = "ai") -> None:
        """Record a message.

        Args:
            content: Message content.
            role: Message role ('ai' or 'user').
        """
        if not self._recording:
            return

        self._step += 1
        self._session.actions.append(
            ReplayAction(
                step=self._step,
                action_type=f"{role}_message",
                content=content[:500],
            )
        )

    def _generalize_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Generalize tool arguments by replacing absolute paths with variables.

        Args:
            args: Original tool arguments.

        Returns:
            Generalized arguments.
        """
        generalized = {}
        cwd = self._session.original_context.get("cwd", "")

        for key, value in args.items():
            if isinstance(value, str) and cwd and value.startswith(cwd):
                # Replace absolute path with variable
                relative = value[len(cwd) :]
                generalized[key] = f"${{CWD}}{relative}"
                self._session.variables["CWD"] = cwd
            else:
                generalized[key] = value

        return generalized


def save_replay_session(config_dir: Path, session: ReplaySession) -> Path:
    """Save a replay session to disk.

    Args:
        config_dir: Config directory.
        session: Session to save.

    Returns:
        Path to saved file.
    """
    replays_dir = config_dir / "replays"
    replays_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "session_id": session.session_id,
        "name": session.name,
        "description": session.description,
        "recorded_at": session.recorded_at,
        "original_context": session.original_context,
        "variables": session.variables,
        "actions": [
            {
                "step": a.step,
                "action_type": a.action_type,
                "tool_name": a.tool_name,
                "tool_args": a.tool_args,
                "content": a.content,
                "result_pattern": a.result_pattern,
            }
            for a in session.actions
        ],
    }

    file_path = replays_dir / f"{session.session_id}.json"
    file_path.write_text(json.dumps(data, indent=2))
    return file_path


def load_replay_session(file_path: Path) -> ReplaySession:
    """Load a replay session from disk.

    Args:
        file_path: Path to the session file.

    Returns:
        Loaded ReplaySession.
    """
    data = json.loads(file_path.read_text())

    return ReplaySession(
        session_id=data["session_id"],
        name=data.get("name", ""),
        description=data.get("description", ""),
        recorded_at=data.get("recorded_at", 0),
        original_context=data.get("original_context", {}),
        variables=data.get("variables", {}),
        actions=[
            ReplayAction(
                step=a["step"],
                action_type=a["action_type"],
                tool_name=a.get("tool_name", ""),
                tool_args=a.get("tool_args", {}),
                content=a.get("content", ""),
                result_pattern=a.get("result_pattern", ""),
            )
            for a in data.get("actions", [])
        ],
    )


def list_replay_sessions(config_dir: Path) -> list[ReplaySession]:
    """List all saved replay sessions.

    Args:
        config_dir: Config directory.

    Returns:
        List of saved sessions.
    """
    replays_dir = config_dir / "replays"
    if not replays_dir.exists():
        return []

    sessions = []
    for file_path in sorted(replays_dir.glob("*.json")):
        try:
            sessions.append(load_replay_session(file_path))
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning("Failed to load replay session %s: %s", file_path, e)

    return sessions


def generate_replay_prompt(
    session: ReplaySession,
    new_context: dict[str, Any],
) -> str:
    """Generate a prompt for replaying a session in a new context.

    Adapts the recorded actions to the new context by substituting
    variables and generating instructions the agent can follow.

    Args:
        session: The recorded session to replay.
        new_context: New context (cwd, git info, etc.).

    Returns:
        Prompt string for the agent.
    """
    lines = [
        f"# Replay: {session.name or session.session_id}",
        "",
        f"{session.description}"
        if session.description
        else "Replaying a recorded session.",
        "",
        "## Instructions",
        "",
        "Follow these steps, adapting paths and arguments to the current context:",
        "",
    ]

    new_cwd = new_context.get("cwd", "")

    for action in session.actions:
        if action.action_type == "tool_call":
            # Substitute variables
            adapted_args = {}
            for key, value in action.tool_args.items():
                if isinstance(value, str) and "${CWD}" in value:
                    adapted_args[key] = value.replace("${CWD}", new_cwd)
                else:
                    adapted_args[key] = value

            args_str = ", ".join(f"{k}={v!r}" for k, v in adapted_args.items())
            lines.append(f"{action.step}. Call `{action.tool_name}({args_str})`")

            if action.result_pattern:
                lines.append(f"   Expected: {action.result_pattern}")
            lines.append("")

        elif action.action_type == "user_message":
            lines.append(f'{action.step}. User said: "{action.content}"')
            lines.append("")

    lines.extend(
        [
            "",
            "## Adaptation Notes",
            "",
            "- Verify each step succeeds before proceeding",
            "- If a step fails, investigate the difference from the original context",
            "- File paths have been adapted but may need further adjustment",
        ]
    )

    return "\n".join(lines)
