"""Collaborative Sessions middleware for multi-advisor agent sessions."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


@dataclass
class Participant:
    """A participant in a collaborative session."""

    user_id: str
    name: str
    role: str
    joined_at: str
    is_active: bool = True


@dataclass
class SessionMessage:
    """A message within a collaborative session."""

    msg_id: int
    sender_id: str
    content: str
    timestamp: str
    msg_type: str = "chat"  # chat, annotation, action, system


@dataclass
class CollaborativeSession:
    """A collaborative advisor session."""

    session_id: int
    title: str
    participants: list[Participant] = field(default_factory=list)
    messages: list[SessionMessage] = field(default_factory=list)
    created_at: str = ""
    _next_msg_id: int = 1

    def add_message(self, sender_id: str, content: str, msg_type: str = "chat") -> SessionMessage:
        """Add a message to the session."""
        msg = SessionMessage(
            msg_id=self._next_msg_id,
            sender_id=sender_id,
            content=content,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            msg_type=msg_type,
        )
        self._next_msg_id += 1
        self.messages.append(msg)
        return msg


@dataclass
class CollabStore:
    """Storage for collaborative sessions."""

    sessions: dict[int, CollaborativeSession] = field(default_factory=dict)
    active_session_id: int | None = None
    _next_session_id: int = 1

    def create_session(self, title: str) -> CollaborativeSession:
        """Create a new collaborative session."""
        session = CollaborativeSession(
            session_id=self._next_session_id,
            title=title,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.sessions[self._next_session_id] = session
        self.active_session_id = self._next_session_id
        self._next_session_id += 1
        return session


SYSTEM_PROMPT = """You have access to collaborative session tools that allow multiple advisors \
to participate in the same agent session. Use these tools to create sessions, add participants, \
exchange messages, and review transcripts. Message types include: chat, annotation, action, system."""


class CollaborativeSessionsState(TypedDict):
    """State for collaborative sessions middleware."""


class CollaborativeSessionsMiddleware(AgentMiddleware[CollaborativeSessionsState, ContextT, ResponseT]):
    """Middleware enabling multi-advisor collaborative sessions."""

    state_schema = CollaborativeSessionsState

    def __init__(self) -> None:
        self.store = CollabStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build the collaborative session tools."""
        mw = self

        def create_collab_session(
            runtime: ToolRuntime[None, CollaborativeSessionsState],
            title: Annotated[str, "Title for the collaborative session"],
            creator_name: Annotated[str, "Name of the session creator"],
            creator_role: Annotated[str, "Role of the session creator"],
        ) -> str:
            """Create a new collaborative session with an initial participant."""
            session = mw.store.create_session(title)
            participant = Participant(
                user_id="user_1",
                name=creator_name,
                role=creator_role,
                joined_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            session.participants.append(participant)
            session.add_message("system", f"Session '{title}' created by {creator_name}.", msg_type="system")
            logger.info("Created collaborative session %d: %s", session.session_id, title)
            return f"Created session #{session.session_id}: '{title}' with creator {creator_name} ({creator_role})."

        def join_session(
            runtime: ToolRuntime[None, CollaborativeSessionsState],
            session_id: Annotated[int, "ID of the session to join"],
            name: Annotated[str, "Name of the participant joining"],
            role: Annotated[str, "Role of the participant"],
        ) -> str:
            """Join an existing collaborative session."""
            session = mw.store.sessions.get(session_id)
            if not session:
                return f"Session #{session_id} not found."
            user_id = f"user_{len(session.participants) + 1}"
            participant = Participant(
                user_id=user_id,
                name=name,
                role=role,
                joined_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            session.participants.append(participant)
            session.add_message("system", f"{name} joined the session as {role}.", msg_type="system")
            logger.info("Participant %s joined session %d", name, session_id)
            return f"{name} joined session #{session_id} as {role} (user_id: {user_id})."

        def send_message(
            runtime: ToolRuntime[None, CollaborativeSessionsState],
            session_id: Annotated[int, "ID of the session"],
            sender_id: Annotated[str, "User ID of the sender"],
            content: Annotated[str, "Message content"],
            msg_type: Annotated[str, "Message type: chat, annotation, action, or system"] = "chat",
        ) -> str:
            """Send a message in a collaborative session."""
            session = mw.store.sessions.get(session_id)
            if not session:
                return f"Session #{session_id} not found."
            if msg_type not in ("chat", "annotation", "action", "system"):
                return f"Invalid message type: {msg_type}. Must be chat, annotation, action, or system."
            msg = session.add_message(sender_id, content, msg_type)
            logger.info("Message %d sent in session %d by %s", msg.msg_id, session_id, sender_id)
            return f"Message #{msg.msg_id} sent in session #{session_id} [{msg_type}]."

        def session_transcript(
            runtime: ToolRuntime[None, CollaborativeSessionsState],
            session_id: Annotated[int, "ID of the session"],
        ) -> str:
            """Get the full transcript of a collaborative session."""
            session = mw.store.sessions.get(session_id)
            if not session:
                return f"Session #{session_id} not found."
            lines = [f"# Session #{session.session_id}: {session.title}", f"Created: {session.created_at}", ""]
            lines.append("## Participants")
            for p in session.participants:
                status = "active" if p.is_active else "inactive"
                lines.append(f"- {p.name} ({p.role}) [{status}]")
            lines.append("")
            lines.append("## Messages")
            if not session.messages:
                lines.append("No messages yet.")
            else:
                for m in session.messages:
                    lines.append(f"[{m.timestamp}] ({m.msg_type}) {m.sender_id}: {m.content}")
            return "\n".join(lines)

        def clear_sessions(
            runtime: ToolRuntime[None, CollaborativeSessionsState],
        ) -> str:
            """Clear all collaborative sessions."""
            count = len(mw.store.sessions)
            mw.store.sessions.clear()
            mw.store.active_session_id = None
            mw.store._next_session_id = 1
            logger.info("Cleared %d collaborative sessions", count)
            return f"Cleared {count} collaborative session(s)."

        return [
            StructuredTool.from_function(
                func=create_collab_session,
                name="create_collab_session",
                description="Create a new collaborative session with an initial participant.",
            ),
            StructuredTool.from_function(
                func=join_session,
                name="join_session",
                description="Join an existing collaborative session.",
            ),
            StructuredTool.from_function(
                func=send_message,
                name="send_message",
                description="Send a message in a collaborative session.",
            ),
            StructuredTool.from_function(
                func=session_transcript,
                name="session_transcript",
                description="Get the full transcript of a collaborative session.",
            ),
            StructuredTool.from_function(
                func=clear_sessions,
                name="clear_sessions",
                description="Clear all collaborative sessions.",
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append collaborative sessions system prompt to the request."""
        return request.override(
            system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT),
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Wrap synchronous model call with collaborative sessions context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Wrap asynchronous model call with collaborative sessions context."""
        return await call_next(self.modify_request(request))


__all__ = [
    "CollabStore",
    "CollaborativeSession",
    "CollaborativeSessionsMiddleware",
    "Participant",
    "SessionMessage",
]
