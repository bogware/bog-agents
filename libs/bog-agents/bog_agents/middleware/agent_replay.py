"""Agent Replay / Time Travel middleware for recording and replaying sessions.

Records every agent action with full state, enabling step-by-step replay,
forking from any point, and debugging exactly where an agent went wrong.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ActionType(StrEnum):
    """Types of recorded agent actions."""

    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    STATE_CHANGE = "state_change"
    ERROR = "error"
    CHECKPOINT = "checkpoint"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DECISION = "approval_decision"


@dataclass
class ReplayAction:
    """A single recorded action in a replay session."""

    action_id: int
    action_type: ActionType
    timestamp: float
    data: dict[str, Any]
    parent_id: int | None = None
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "timestamp": self.timestamp,
            "data": self.data,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplayAction:
        """Deserialize from dictionary.

        Args:
            d: Serialized action data.

        Returns:
            ReplayAction instance.
        """
        return cls(
            action_id=d["action_id"],
            action_type=ActionType(d["action_type"]),
            timestamp=d["timestamp"],
            data=d["data"],
            parent_id=d.get("parent_id"),
            duration_ms=d.get("duration_ms"),
            metadata=d.get("metadata", {}),
        )


@dataclass
class ReplaySession:
    """A complete recorded session for replay."""

    session_id: str
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    actions: list[ReplayAction] = field(default_factory=list)
    _next_id: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        action_type: ActionType,
        data: dict[str, Any],
        *,
        parent_id: int | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ReplayAction:
        """Record a new action.

        Args:
            action_type: Type of action.
            data: Action data (tool name, args, result, etc.).
            parent_id: ID of the parent action (for nesting).
            duration_ms: How long the action took.
            metadata: Additional metadata.

        Returns:
            The recorded ReplayAction.
        """
        action = ReplayAction(
            action_id=self._next_id,
            action_type=action_type,
            timestamp=time.time(),
            data=data,
            parent_id=parent_id,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        self.actions.append(action)
        self._next_id += 1
        return action

    @property
    def total_actions(self) -> int:
        """Total number of recorded actions."""
        return len(self.actions)

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        if not self.actions:
            return 0.0
        end = self.ended_at or time.time()
        return end - self.started_at

    def get_action(self, action_id: int) -> ReplayAction | None:
        """Get a specific action by ID.

        Args:
            action_id: Action ID.

        Returns:
            The action, or None if not found.
        """
        for action in self.actions:
            if action.action_id == action_id:
                return action
        return None

    def get_actions_range(self, start_id: int, end_id: int) -> list[ReplayAction]:
        """Get actions within an ID range.

        Args:
            start_id: Start action ID (inclusive).
            end_id: End action ID (inclusive).

        Returns:
            List of actions in range.
        """
        return [a for a in self.actions if start_id <= a.action_id <= end_id]

    def fork_at(self, action_id: int) -> ReplaySession:
        """Fork a new session from a specific point in time.

        Creates a new session containing all actions up to and including
        the specified action_id. The fork can then diverge independently.

        Args:
            action_id: Action ID to fork from (inclusive).

        Returns:
            New ReplaySession containing actions up to the fork point.
        """
        forked_actions = [a for a in self.actions if a.action_id <= action_id]
        import uuid

        fork = ReplaySession(
            session_id=f"{self.session_id}-fork-{str(uuid.uuid4())[:8]}",
            started_at=self.started_at,
            actions=list(forked_actions),
            _next_id=action_id + 1,
            metadata={
                **self.metadata,
                "forked_from": self.session_id,
                "fork_point": action_id,
            },
        )
        return fork

    def get_tool_calls(self) -> list[ReplayAction]:
        """Get all tool call actions.

        Returns:
            List of tool call actions.
        """
        return [a for a in self.actions if a.action_type == ActionType.TOOL_CALL]

    def get_errors(self) -> list[ReplayAction]:
        """Get all error actions.

        Returns:
            List of error actions.
        """
        return [a for a in self.actions if a.action_type == ActionType.ERROR]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "actions": [a.to_dict() for a in self.actions],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReplaySession:
        """Deserialize from dictionary.

        Args:
            d: Serialized session data.

        Returns:
            ReplaySession instance.
        """
        actions = [ReplayAction.from_dict(a) for a in d.get("actions", [])]
        session = cls(
            session_id=d["session_id"],
            started_at=d.get("started_at", time.time()),
            ended_at=d.get("ended_at"),
            actions=actions,
            _next_id=max((a.action_id for a in actions), default=-1) + 1,
            metadata=d.get("metadata", {}),
        )
        return session

    def to_timeline(self) -> str:
        """Generate a human-readable timeline of the session.

        Returns:
            Formatted timeline string.
        """
        if not self.actions:
            return "Empty session"

        lines: list[str] = []
        lines.append(f"Session: {self.session_id}")
        lines.append(f"Duration: {self.duration_seconds:.1f}s")
        lines.append(f"Actions: {self.total_actions}")
        lines.append("")

        for action in self.actions:
            elapsed = action.timestamp - self.started_at
            prefix = f"[{elapsed:7.1f}s] #{action.action_id:3d}"
            duration_str = f" ({action.duration_ms:.0f}ms)" if action.duration_ms else ""

            if action.action_type == ActionType.TOOL_CALL:
                tool_name = action.data.get("tool_name", "unknown")
                lines.append(f"{prefix} TOOL  {tool_name}{duration_str}")
            elif action.action_type == ActionType.TOOL_RESULT:
                success = action.data.get("success", True)
                status = "OK" if success else "FAIL"
                lines.append(f"{prefix} RESULT {status}{duration_str}")
            elif action.action_type == ActionType.MODEL_CALL:
                lines.append(f"{prefix} MODEL{duration_str}")
            elif action.action_type == ActionType.ERROR:
                error = action.data.get("error", "unknown")
                lines.append(f"{prefix} ERROR {error[:60]}")
            elif action.action_type == ActionType.USER_MESSAGE:
                msg = action.data.get("content", "")[:60]
                lines.append(f"{prefix} USER  {msg}")
            elif action.action_type == ActionType.ASSISTANT_MESSAGE:
                msg = action.data.get("content", "")[:60]
                lines.append(f"{prefix} AGENT {msg}")
            else:
                lines.append(f"{prefix} {action.action_type}")

        return "\n".join(lines)


class ReplayStore:
    """Persistence layer for replay sessions."""

    def __init__(self, store_dir: str | None = None) -> None:
        """Initialize the replay store.

        Args:
            store_dir: Directory for storing replay files.
        """
        if store_dir is None:
            store_dir = "~/.bog-agents/replays"
        self.store_dir = Path(store_dir).expanduser()
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def save(self, session: ReplaySession) -> Path:
        """Save a replay session to disk.

        Args:
            session: The session to save.

        Returns:
            Path to the saved file.
        """
        file_path = self.store_dir / f"{session.session_id}.json"
        file_path.write_text(json.dumps(session.to_dict(), indent=2))
        logger.info("Saved replay: %s (%d actions)", session.session_id, session.total_actions)
        return file_path

    def load(self, session_id: str) -> ReplaySession | None:
        """Load a replay session from disk.

        Args:
            session_id: Session ID to load.

        Returns:
            ReplaySession or None if not found.
        """
        file_path = self.store_dir / f"{session_id}.json"
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text())
            return ReplaySession.from_dict(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load replay %s: %s", session_id, exc)
            return None

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """List available replay sessions.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            List of session summaries (id, started_at, action_count).
        """
        sessions: list[dict[str, Any]] = []
        for file_path in sorted(self.store_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if len(sessions) >= limit:
                break
            try:
                data = json.loads(file_path.read_text())
                sessions.append({
                    "session_id": data["session_id"],
                    "started_at": data.get("started_at"),
                    "action_count": len(data.get("actions", [])),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return sessions

    def delete(self, session_id: str) -> bool:
        """Delete a replay session.

        Args:
            session_id: Session to delete.

        Returns:
            True if found and deleted.
        """
        file_path = self.store_dir / f"{session_id}.json"
        if file_path.exists():
            file_path.unlink()
            return True
        return False


class AgentReplayMiddleware(AgentMiddleware):
    """Middleware for recording and replaying agent sessions.

    Records every action with full state for step-by-step replay,
    time-travel debugging, and session forking.

    Example:
        ```python
        from bog_agents.middleware.agent_replay import AgentReplayMiddleware

        middleware = AgentReplayMiddleware(
            session_id="my-session",
            auto_save=True,
        )

        # After the session
        timeline = middleware.get_timeline()
        print(timeline)

        # Fork from action #5 and try a different approach
        forked = middleware.fork_at(5)
        ```
    """

    session: ReplaySession
    store: ReplayStore
    auto_save: bool
    recording: bool

    def __init__(
        self,
        *,
        session_id: str | None = None,
        store_dir: str | None = None,
        auto_save: bool = True,
        recording: bool = True,
    ) -> None:
        """Initialize agent replay middleware.

        Args:
            session_id: Session identifier. Auto-generated if None.
            store_dir: Directory for replay storage.
            auto_save: Whether to auto-save after each action.
            recording: Whether recording is active.
        """
        if session_id is None:
            import uuid
            session_id = str(uuid.uuid4())[:12]

        self.session = ReplaySession(session_id=session_id)
        self.store = ReplayStore(store_dir)
        self.auto_save = auto_save
        self.recording = recording

    def record_tool_call(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        parent_id: int | None = None,
    ) -> int:
        """Record a tool call.

        Args:
            tool_name: Name of the tool.
            tool_args: Tool arguments.
            parent_id: Parent action ID.

        Returns:
            Action ID for this tool call.
        """
        if not self.recording:
            return -1

        action = self.session.record(
            ActionType.TOOL_CALL,
            {"tool_name": tool_name, "tool_args": tool_args},
            parent_id=parent_id,
        )
        return action.action_id

    def record_tool_result(
        self,
        result: Any,
        *,
        success: bool = True,
        parent_id: int | None = None,
        duration_ms: float | None = None,
    ) -> int:
        """Record a tool result.

        Args:
            result: Tool output.
            success: Whether the tool succeeded.
            parent_id: Parent tool call action ID.
            duration_ms: Execution time in milliseconds.

        Returns:
            Action ID.
        """
        if not self.recording:
            return -1

        data: dict[str, Any] = {"success": success}
        if isinstance(result, str):
            data["result"] = result[:5000]  # Truncate large outputs
        elif isinstance(result, dict):
            data["result"] = result
        else:
            data["result"] = str(result)[:5000]

        action = self.session.record(
            ActionType.TOOL_RESULT,
            data,
            parent_id=parent_id,
            duration_ms=duration_ms,
        )
        return action.action_id

    def record_error(self, error: str, *, parent_id: int | None = None) -> int:
        """Record an error.

        Args:
            error: Error description.
            parent_id: Parent action ID.

        Returns:
            Action ID.
        """
        if not self.recording:
            return -1

        action = self.session.record(
            ActionType.ERROR,
            {"error": error},
            parent_id=parent_id,
        )
        return action.action_id

    def fork_at(self, action_id: int) -> ReplaySession:
        """Fork the session at a specific action.

        Args:
            action_id: Action ID to fork from.

        Returns:
            New forked ReplaySession.
        """
        return self.session.fork_at(action_id)

    def get_timeline(self) -> str:
        """Get a human-readable timeline.

        Returns:
            Formatted timeline string.
        """
        return self.session.to_timeline()

    def save(self) -> Path:
        """Save the current session.

        Returns:
            Path to saved file.
        """
        self.session.ended_at = time.time()
        return self.store.save(self.session)

    def load_session(self, session_id: str) -> ReplaySession | None:
        """Load a previously saved session.

        Args:
            session_id: Session to load.

        Returns:
            Loaded ReplaySession or None.
        """
        return self.store.load(session_id)

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Record model calls in the replay session."""
        start = time.time()

        if self.recording:
            self.session.record(
                ActionType.MODEL_CALL,
                {"message_count": len(request.messages) if hasattr(request, "messages") else 0},
            )

        try:
            response = await call_next(request, runtime)
        except Exception as exc:
            if self.recording:
                self.session.record(
                    ActionType.ERROR,
                    {"error": str(exc), "error_type": type(exc).__name__},
                )
            raise

        duration_ms = (time.time() - start) * 1000
        if self.recording and self.auto_save and self.session.total_actions % 10 == 0:
            self.store.save(self.session)

        return response
