"""Messaging integration middleware for Slack, Teams, Email, and webhook outputs."""

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
class MessagingChannel:
    """A registered messaging channel."""

    channel_id: int
    platform: str  # slack, teams, email, webhook
    channel_name: str
    webhook_url: str
    is_active: bool = True


@dataclass
class OutboundMessage:
    """A message sent to an external channel."""

    msg_id: int
    channel_id: int
    content: str
    format: str = "text"  # text, markdown, html
    sent_at: str = ""
    status: str = "pending"  # pending, sent, failed


@dataclass
class MessagingStore:
    """Storage for messaging channels and outbound messages."""

    channels: dict[int, MessagingChannel] = field(default_factory=dict)
    messages: list[OutboundMessage] = field(default_factory=list)
    _next_channel_id: int = 1
    _next_msg_id: int = 1

    def register_channel(self, platform: str, channel_name: str, webhook_url: str) -> MessagingChannel:
        """Register a new messaging channel."""
        channel = MessagingChannel(
            channel_id=self._next_channel_id,
            platform=platform,
            channel_name=channel_name,
            webhook_url=webhook_url,
        )
        self.channels[self._next_channel_id] = channel
        self._next_channel_id += 1
        return channel

    def send_message(self, channel_id: int, content: str, fmt: str = "text") -> OutboundMessage:
        """Queue an outbound message to a channel."""
        msg = OutboundMessage(
            msg_id=self._next_msg_id,
            channel_id=channel_id,
            content=content,
            format=fmt,
            sent_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            status="sent",
        )
        self._next_msg_id += 1
        self.messages.append(msg)
        return msg


SYSTEM_PROMPT = """You have access to messaging integration tools for sending agent outputs to \
external platforms. Supported platforms: slack, teams, email, webhook. Supported formats: text, \
markdown, html. Use these tools to register channels, send messages, and review message history."""


class MessagingIntegrationState(TypedDict):
    """State for messaging integration middleware."""


class MessagingIntegrationMiddleware(AgentMiddleware[MessagingIntegrationState, ContextT, ResponseT]):
    """Middleware for integrating with Slack, Teams, Email, and webhook platforms."""

    state_schema = MessagingIntegrationState

    def __init__(self) -> None:
        self.store = MessagingStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build the messaging integration tools."""
        mw = self

        def register_channel(
            runtime: ToolRuntime[None, MessagingIntegrationState],
            platform: Annotated[str, "Platform: slack, teams, email, or webhook"],
            channel_name: Annotated[str, "Display name for the channel"],
            webhook_url: Annotated[str, "Webhook URL or endpoint for delivery"],
        ) -> str:
            """Register a new messaging channel for output delivery."""
            if platform not in ("slack", "teams", "email", "webhook"):
                return f"Invalid platform: {platform}. Must be slack, teams, email, or webhook."
            channel = mw.store.register_channel(platform, channel_name, webhook_url)
            logger.info("Registered channel %d: %s (%s)", channel.channel_id, channel_name, platform)
            return f"Registered channel #{channel.channel_id}: '{channel_name}' on {platform} (webhook: {webhook_url})."

        def send_to_channel(
            runtime: ToolRuntime[None, MessagingIntegrationState],
            channel_id: Annotated[int, "ID of the channel to send to"],
            content: Annotated[str, "Message content to send"],
            fmt: Annotated[str, "Message format: text, markdown, or html"] = "text",
        ) -> str:
            """Send a message to a registered channel."""
            channel = mw.store.channels.get(channel_id)
            if not channel:
                return f"Channel #{channel_id} not found."
            if not channel.is_active:
                return f"Channel #{channel_id} is inactive."
            if fmt not in ("text", "markdown", "html"):
                return f"Invalid format: {fmt}. Must be text, markdown, or html."
            msg = mw.store.send_message(channel_id, content, fmt)
            logger.info("Sent message %d to channel %d (%s)", msg.msg_id, channel_id, channel.platform)
            return f"Message #{msg.msg_id} sent to '{channel.channel_name}' ({channel.platform}) [{fmt}] — status: {msg.status}."

        def list_channels(
            runtime: ToolRuntime[None, MessagingIntegrationState],
        ) -> str:
            """List all registered messaging channels."""
            if not mw.store.channels:
                return "No messaging channels registered."
            lines = ["# Registered Channels", ""]
            for ch in mw.store.channels.values():
                status = "active" if ch.is_active else "inactive"
                lines.append(f"- #{ch.channel_id}: {ch.channel_name} ({ch.platform}) [{status}]")
            return "\n".join(lines)

        def message_history(
            runtime: ToolRuntime[None, MessagingIntegrationState],
            channel_id: Annotated[int, "ID of the channel to get history for (0 for all)"] = 0,
        ) -> str:
            """Get outbound message history, optionally filtered by channel."""
            msgs = mw.store.messages
            if channel_id:
                msgs = [m for m in msgs if m.channel_id == channel_id]
            if not msgs:
                return "No messages found."
            lines = ["# Message History", ""]
            for m in msgs:
                channel = mw.store.channels.get(m.channel_id)
                ch_name = channel.channel_name if channel else "unknown"
                lines.append(
                    f"- #{m.msg_id} -> {ch_name} [{m.format}] ({m.status}) at {m.sent_at}: {m.content[:80]}{'...' if len(m.content) > 80 else ''}"
                )
            return "\n".join(lines)

        def clear_messaging(
            runtime: ToolRuntime[None, MessagingIntegrationState],
        ) -> str:
            """Clear all channels and message history."""
            ch_count = len(mw.store.channels)
            msg_count = len(mw.store.messages)
            mw.store.channels.clear()
            mw.store.messages.clear()
            mw.store._next_channel_id = 1
            mw.store._next_msg_id = 1
            logger.info("Cleared %d channels and %d messages", ch_count, msg_count)
            return f"Cleared {ch_count} channel(s) and {msg_count} message(s)."

        return [
            StructuredTool.from_function(
                func=register_channel,
                name="register_channel",
                description="Register a new messaging channel for output delivery.",
            ),
            StructuredTool.from_function(
                func=send_to_channel,
                name="send_to_channel",
                description="Send a message to a registered channel.",
            ),
            StructuredTool.from_function(
                func=list_channels,
                name="list_channels",
                description="List all registered messaging channels.",
            ),
            StructuredTool.from_function(
                func=message_history,
                name="message_history",
                description="Get outbound message history.",
            ),
            StructuredTool.from_function(
                func=clear_messaging,
                name="clear_messaging",
                description="Clear all channels and message history.",
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append messaging integration system prompt to the request."""
        return request.override(
            system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT),
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Wrap synchronous model call with messaging integration context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Wrap asynchronous model call with messaging integration context."""
        return await call_next(self.modify_request(request))


__all__ = [
    "MessagingChannel",
    "MessagingIntegrationMiddleware",
    "MessagingStore",
    "OutboundMessage",
]
