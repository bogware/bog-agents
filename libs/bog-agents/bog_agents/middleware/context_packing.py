"""Middleware for smart context packing / structured compression.

Feature #48: Smart context packing — instead of LLM summarization, compress
old messages into structured data (AST diffs, file state snapshots) that
are cheaper to re-expand. Preserves more signal at lower token cost.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class FileStateSnapshot:
    """Snapshot of a file's state at a point in time."""

    path: str
    exists: bool = True
    line_count: int = 0
    last_edit_description: str = ""
    symbols: list[str] = field(default_factory=list)


@dataclass
class ToolCallSummary:
    """Compact representation of a tool call and its result."""

    tool_name: str
    key_args: dict[str, str] = field(default_factory=dict)
    result_summary: str = ""
    success: bool = True


@dataclass
class ConversationSegment:
    """A compressed segment of conversation history."""

    turn_range: tuple[int, int]
    """Range of message indices this segment covers."""

    user_intent: str
    """Summarized user intent for this segment."""

    actions_taken: list[ToolCallSummary] = field(default_factory=list)
    """Compact tool call summaries."""

    files_modified: list[FileStateSnapshot] = field(default_factory=list)
    """File state changes during this segment."""

    key_decisions: list[str] = field(default_factory=list)
    """Important decisions or findings."""

    errors_encountered: list[str] = field(default_factory=list)
    """Errors that occurred."""


def _extract_tool_summary(message: ToolMessage) -> ToolCallSummary:
    """Extract a compact summary from a tool message.

    Args:
        message: The tool message to summarize.

    Returns:
        Compact tool call summary.
    """
    content = str(message.content) if message.content else ""

    # Truncate long results to key info
    if len(content) > 200:  # noqa: PLR2004
        content = content[:200] + "..."

    success = "error" not in content.lower() and "failed" not in content.lower()

    return ToolCallSummary(
        tool_name=message.name or "unknown",
        result_summary=content,
        success=success,
    )


def _extract_file_operations(messages: list[BaseMessage]) -> list[FileStateSnapshot]:
    """Extract file state information from a sequence of messages.

    Args:
        messages: Messages to analyze for file operations.

    Returns:
        List of file state snapshots.
    """
    files: dict[str, FileStateSnapshot] = {}

    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue

        content = str(msg.content) if msg.content else ""
        name = msg.name or ""

        if name in ("write_file", "edit_file", "multi_edit_file"):
            # Extract file path from content
            for line in content.splitlines():
                if "Updated file" in line or "replaced" in line.lower():
                    # Try to extract path
                    parts = line.split("'")
                    if len(parts) >= 2:
                        path = parts[1]
                        files[path] = FileStateSnapshot(
                            path=path,
                            last_edit_description=line[:100],
                        )

    return list(files.values())


def pack_messages(
    messages: list[BaseMessage],
    *,
    max_packed_tokens: int = 2000,
) -> str:
    """Pack a sequence of messages into a structured, compact representation.

    Instead of LLM summarization, this extracts structured data:
    - User intents from human messages
    - Tool call summaries (name, key args, result status)
    - File state snapshots
    - Key decisions from AI messages

    Args:
        messages: Messages to pack.
        max_packed_tokens: Approximate token budget for packed output.

    Returns:
        Structured packed representation as a string.
    """
    if not messages:
        return "[No messages to pack]"

    # Extract user intents
    user_intents: list[str] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            content = str(msg.content) if msg.content else ""
            if len(content) > 150:  # noqa: PLR2004
                content = content[:150] + "..."
            user_intents.append(content)

    # Extract tool summaries
    tool_summaries: list[ToolCallSummary] = []
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_summaries.append(_extract_tool_summary(msg))

    # Extract file operations
    file_states = _extract_file_operations(messages)

    # Extract key AI decisions/reasoning
    decisions: list[str] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            content = str(msg.content) if msg.content else ""
            # Extract first sentence or first 100 chars as key decision
            if content:
                first_line = content.split("\n")[0]
                if len(first_line) > 100:  # noqa: PLR2004
                    first_line = first_line[:100] + "..."
                if first_line.strip():
                    decisions.append(first_line)

    # Build packed representation
    parts: list[str] = []

    parts.append("=== Packed Context ===")

    if user_intents:
        parts.append("\n## User Requests")
        for i, intent in enumerate(user_intents[:5], 1):
            parts.append(f"{i}. {intent}")

    if tool_summaries:
        parts.append("\n## Actions Taken")
        for ts in tool_summaries[:20]:
            status = "OK" if ts.success else "FAIL"
            summary = ts.result_summary[:80] if ts.result_summary else ""
            parts.append(f"- {ts.tool_name} [{status}] {summary}")

    if file_states:
        parts.append("\n## Files Modified")
        for fs in file_states[:20]:
            parts.append(f"- {fs.path}: {fs.last_edit_description}")

    if decisions:
        parts.append("\n## Key Decisions")
        for d in decisions[:10]:
            parts.append(f"- {d}")

    result = "\n".join(parts)

    # Rough token check (4 chars per token)
    if len(result) > max_packed_tokens * 4:
        result = result[: max_packed_tokens * 4] + "\n... [packed context truncated]"

    return result


class ContextPackingState(TypedDict):
    """State for context packing middleware."""


class ContextPackingMiddleware(AgentMiddleware[ContextPackingState, ContextT, ResponseT]):
    """Middleware that uses structured compression instead of LLM summarization.

    When the context window fills up, instead of calling an LLM to summarize,
    this middleware extracts structured data (tool call summaries, file states,
    user intents) and compresses old messages into a compact representation.

    This is faster, cheaper, and preserves more structured signal than
    LLM-generated summaries.

    Args:
        threshold_pct: Percentage of context window at which to trigger packing.
        max_packed_tokens: Token budget for packed output.
        context_window: Context window size in tokens.
    """

    state_schema = ContextPackingState

    def __init__(
        self,
        *,
        threshold_pct: float = 0.6,
        max_packed_tokens: int = 2000,
        context_window: int = 200_000,
    ) -> None:
        self._threshold_pct = threshold_pct
        self._max_packed_tokens = max_packed_tokens
        self._context_window = context_window

    def _estimate_tokens(self, messages: list[BaseMessage]) -> int:
        """Rough token estimate for a list of messages.

        Args:
            messages: Messages to estimate.

        Returns:
            Approximate token count.
        """
        total_chars = sum(len(str(msg.content) if msg.content else "") for msg in messages)
        return total_chars // 4

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Check context usage and pack old messages if needed."""
        if hasattr(request, "messages"):
            messages = request.messages
            estimated = self._estimate_tokens(messages)
            threshold = int(self._context_window * self._threshold_pct)

            if estimated > threshold and len(messages) > 10:  # noqa: PLR2004
                # Pack the older messages, keep recent ones
                keep_count = min(10, len(messages) // 3)
                old_messages = messages[:-keep_count]
                recent_messages = messages[-keep_count:]

                packed = pack_messages(
                    old_messages,
                    max_packed_tokens=self._max_packed_tokens,
                )

                # Replace old messages with packed context
                packed_msg = SystemMessage(content=f"[Previous conversation packed]\n{packed}")
                request.messages = [packed_msg, *recent_messages]

                logger.info(
                    "Packed %d messages into structured context (%d -> ~%d tokens)",
                    len(old_messages),
                    estimated,
                    self._estimate_tokens(request.messages),
                )

        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        if hasattr(request, "messages"):
            messages = request.messages
            estimated = self._estimate_tokens(messages)
            threshold = int(self._context_window * self._threshold_pct)

            if estimated > threshold and len(messages) > 10:  # noqa: PLR2004
                keep_count = min(10, len(messages) // 3)
                old_messages = messages[:-keep_count]
                recent_messages = messages[-keep_count:]

                packed = pack_messages(
                    old_messages,
                    max_packed_tokens=self._max_packed_tokens,
                )

                packed_msg = SystemMessage(content=f"[Previous conversation packed]\n{packed}")
                request.messages = [packed_msg, *recent_messages]

        return await call_next(request)
