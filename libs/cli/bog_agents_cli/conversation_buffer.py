"""CLI-wide conversation buffer (Wave H).

The TUI stores chat history as Textual widgets, not as a flat list of
LangChain messages — and the agent's LangGraph state isn't easily
accessible from slash-command handlers. This module is the cheap
middle ground: every place that mounts a user or assistant message in
the TUI also records a ``(role, content, ts)`` entry here. Features
that need "the last N turns" (``/sidecar``, ``/expert propose``, and
future watchers) read from the buffer via :func:`recent_messages` —
which converts the entries into the same LangChain message shape
:func:`bog_agents_cli.sidecar.summarize_parent_context` expects.

The buffer is per-cwd (so hopping between projects doesn't bleed) and
bounded (default 200 entries). All reads + writes are thread-safe via
a single lock — slash-command handlers run on worker threads, the
agent loop runs on the asyncio thread, the buffer is the merge point.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_DEFAULT_MAX_ENTRIES = 200


@dataclass(frozen=True)
class ConversationEntry:
    """One recorded conversation turn.

    Attributes:
        role: ``"user"`` | ``"assistant"`` | ``"system"`` |
            ``"tool"`` | ``"app"`` (CLI-side status messages). The
            ``"app"`` role exists because the TUI mounts a lot of
            status text that isn't strictly part of the conversation;
            consumers can filter it out.
        content: The message text.
        at: Wall-clock timestamp (``time.time()``) at record time.
    """

    role: str
    content: str
    at: float = field(default_factory=time.time)


class ConversationBuffer:
    """Bounded thread-safe ring buffer of :class:`ConversationEntry`."""

    def __init__(self, *, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._entries: deque[ConversationEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def record(self, *, role: str, content: str) -> None:
        """Append ``(role, content)`` to the buffer.

        Cheap; safe to call on the hot path.
        """
        if not content:
            return
        with self._lock:
            self._entries.append(ConversationEntry(role=role, content=content))

    def recent(self, limit: int = 12) -> list[ConversationEntry]:
        """Return the last *limit* entries, oldest-first."""
        with self._lock:
            if limit <= 0:
                return []
            return list(self._entries)[-limit:]

    def clear(self) -> None:
        """Drop every buffered entry. Test-only convenience."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        """Total number of entries currently in the buffer."""
        with self._lock:
            return len(self._entries)


# ---------------------------------------------------------------------------
# Per-cwd registry
# ---------------------------------------------------------------------------


_BUFFERS: dict[Path, ConversationBuffer] = {}
_BUFFERS_LOCK = threading.Lock()


def get_buffer(cwd: Path | str) -> ConversationBuffer:
    """Return the per-cwd singleton :class:`ConversationBuffer`."""
    key = Path(cwd).resolve()
    with _BUFFERS_LOCK:
        buf = _BUFFERS.get(key)
        if buf is None:
            buf = ConversationBuffer()
            _BUFFERS[key] = buf
    return buf


def reset_buffers() -> None:
    """Drop every cached buffer. Test-only helper."""
    with _BUFFERS_LOCK:
        _BUFFERS.clear()


# ---------------------------------------------------------------------------
# Adapter: ConversationEntry → LangChain messages (for sidecar)
# ---------------------------------------------------------------------------


def recent_messages(
    cwd: Path | str,
    *,
    limit: int = 12,
    include_roles: Sequence[str] | None = None,
) -> list:
    """Return the last *limit* entries as LangChain messages.

    Args:
        cwd: Per-cwd buffer key.
        limit: How many entries to pull from the buffer (most recent
            window).
        include_roles: When provided, only entries with these roles
            are returned. Default ``("user", "assistant", "tool")``
            — drops CLI-internal ``"app"`` status text so the sidecar
            doesn't summarise it as conversation.

    Returns:
        A list of LangChain message instances suitable for
        :func:`bog_agents_cli.sidecar.summarize_parent_context`. When
        an entry's role doesn't map cleanly to a message class, it
        falls through to ``HumanMessage`` (the safe default).
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    roles = set(include_roles) if include_roles else {"user", "assistant", "tool"}
    buf = get_buffer(cwd)
    entries = [e for e in buf.recent(limit=limit) if e.role in roles]
    out: list = []
    for e in entries:
        if e.role == "user":
            out.append(HumanMessage(content=e.content))
        elif e.role == "assistant":
            out.append(AIMessage(content=e.content))
        elif e.role == "system":
            out.append(SystemMessage(content=e.content))
        elif e.role == "tool":
            out.append(
                ToolMessage(
                    content=e.content,
                    tool_call_id="(buffered)",
                    name="(buffered)",
                )
            )
        else:
            out.append(HumanMessage(content=e.content))
    return out


__all__ = [
    "ConversationBuffer",
    "ConversationEntry",
    "get_buffer",
    "recent_messages",
    "reset_buffers",
]
