"""Intelligent context compaction — auto-compress when near context limit.

Monitors token usage on every model call. When usage exceeds
``auto_threshold_pct`` of the context window, compresses old messages into
a structured representation that preserves code context better than
naive truncation.

Exposes ``get_usage_info()`` for the status bar progress indicator.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import BaseMessage, SystemMessage
from typing_extensions import TypedDict

from bog_agents.middleware.context_packing import pack_messages

logger = logging.getLogger(__name__)

_PROGRESS_WIDTH = 20


@dataclass
class CompactionEvent:
    """Record of a single compaction operation.

    Attributes:
        timestamp: Unix epoch when the compaction occurred.
        messages_before: Number of messages before compaction.
        tokens_before: Estimated token count before compaction.
        tokens_after: Estimated token count after compaction.
        ratio: Fraction of tokens retained (tokens_after / tokens_before).
    """

    timestamp: float = field(default_factory=time.time)
    messages_before: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    ratio: float = 0.0

    @property
    def reduction_pct(self) -> float:
        """Percentage of tokens eliminated by this compaction.

        Returns:
            Float in [0, 100] representing how much context was freed.
        """
        return (1 - self.ratio) * 100


@dataclass
class UsageInfo:
    """Snapshot of context window usage for status bar display.

    Attributes:
        estimated_tokens: Approximate token count for current messages.
        context_window: Total context window size in tokens.
        used_pct: Fraction of context window currently used.
        auto_threshold_pct: Threshold fraction that triggers auto-compaction.
        compaction_count: Number of compactions performed this session.
        last_compression_ratio: Ratio from the most recent compaction, or None.
    """

    estimated_tokens: int
    context_window: int
    used_pct: float
    auto_threshold_pct: float
    compaction_count: int
    last_compression_ratio: float | None

    @property
    def progress_bar(self) -> str:
        """Render an ASCII progress bar showing context window utilisation.

        Returns:
            String of the form ``[██████████░░░░░░░░░░] 50.0%`` where the bar
            is always exactly ``_PROGRESS_WIDTH`` characters wide.
        """
        filled = int(min(self.used_pct, 1.0) * _PROGRESS_WIDTH)
        empty = _PROGRESS_WIDTH - filled
        bar = "█" * filled + "░" * empty
        pct = self.used_pct * 100
        return f"[{bar}] {pct:.1f}%"

    @property
    def near_limit(self) -> bool:
        """Return True when usage is at or above the auto-compaction threshold.

        Returns:
            True if ``used_pct >= auto_threshold_pct``.
        """
        return self.used_pct >= self.auto_threshold_pct


class IntelligentCompactionState(TypedDict):
    """LangGraph state shard for intelligent compaction middleware."""


class IntelligentCompactionMiddleware(AgentMiddleware[IntelligentCompactionState, ContextT, ResponseT]):
    """Middleware that auto-compresses messages when the context window fills up.

    Monitors the estimated token count on every model call. When it exceeds
    ``auto_threshold_pct`` of the configured ``context_window``, old messages
    are packed into a structured representation (via ``pack_messages``) that
    preserves code context better than naive truncation.

    The recent 25% of messages (minimum 6) are always kept verbatim so the
    model retains full detail about the most recent turns.

    Call ``get_usage_info()`` to retrieve live usage stats for a status bar
    or progress indicator.

    Args:
        auto_threshold_pct: Fraction of ``context_window`` at which
            auto-compaction triggers. Default ``0.80`` (80 %).
        max_packed_tokens: Approximate token budget for the packed context
            block injected as a ``SystemMessage``. Default ``3000``.
        context_window: Total context window size in tokens. Default ``200_000``.
        enabled: Whether auto-compaction is active. Can be toggled at runtime
            via ``set_enabled()``. Default ``True``.
    """

    state_schema = IntelligentCompactionState

    def __init__(
        self,
        *,
        auto_threshold_pct: float = 0.80,
        max_packed_tokens: int = 3000,
        context_window: int = 200_000,
        enabled: bool = True,
    ) -> None:
        self._auto_threshold_pct = auto_threshold_pct
        self._max_packed_tokens = max_packed_tokens
        self._context_window = context_window
        self._enabled = enabled
        self._compaction_events: list[CompactionEvent] = []
        self._last_estimated: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Return whether auto-compaction is currently active.

        Returns:
            True if auto-compaction will trigger when the threshold is exceeded.
        """
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        """Enable or disable auto-compaction at runtime.

        Args:
            value: True to enable, False to disable.
        """
        self._enabled = value

    def get_usage_info(self) -> UsageInfo:
        """Return a snapshot of current context window usage.

        Returns:
            ``UsageInfo`` populated from the most recent model call estimates
            and compaction history.
        """
        used_pct = self._last_estimated / self._context_window if self._context_window > 0 else 0.0
        last_ratio: float | None = None
        if self._compaction_events:
            last_ratio = self._compaction_events[-1].ratio
        return UsageInfo(
            estimated_tokens=self._last_estimated,
            context_window=self._context_window,
            used_pct=used_pct,
            auto_threshold_pct=self._auto_threshold_pct,
            compaction_count=len(self._compaction_events),
            last_compression_ratio=last_ratio,
        )

    def compress_now(self, messages: list[BaseMessage]) -> tuple[list[BaseMessage], CompactionEvent]:
        """Compress a message list immediately, regardless of threshold.

        Keeps the most recent 25 % of messages verbatim (minimum 6) and packs
        the rest into a structured ``SystemMessage`` prepended to the list.

        Args:
            messages: Full message list to compress.

        Returns:
            A tuple of ``(compressed_messages, event)`` where
            ``compressed_messages`` is the new message list and ``event``
            records metadata about the operation.
        """
        if not messages:
            logger.debug("IntelligentCompaction.compress_now: called with empty message list; no-op")
            return [], CompactionEvent(messages_before=0, tokens_before=0, tokens_after=0, ratio=1.0)
        tokens_before = self._estimate_tokens(messages)

        keep_count = max(6, len(messages) // 4)
        keep_count = min(keep_count, len(messages))
        old_messages = messages[:-keep_count] if keep_count < len(messages) else []
        recent_messages = messages[-keep_count:] if keep_count < len(messages) else messages

        try:
            packed_text = pack_messages(old_messages, max_packed_tokens=self._max_packed_tokens)
        except Exception as exc:
            logger.warning("IntelligentCompaction: pack_messages failed: %s; skipping compaction", exc)
            return messages, CompactionEvent(
                messages_before=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                ratio=1.0,
            )
        packed_msg = SystemMessage(content=f"[Intelligent Compaction — packed context]\n{packed_text}")
        compressed = [packed_msg, *recent_messages]

        tokens_after = self._estimate_tokens(compressed)
        ratio = tokens_after / tokens_before if tokens_before > 0 else 1.0

        event = CompactionEvent(
            messages_before=len(messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            ratio=ratio,
        )
        self._compaction_events.append(event)
        return compressed, event

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _estimate_tokens(self, messages: list[BaseMessage]) -> int:
        """Estimate token count for a list of messages.

        Uses a simple heuristic: 1 token ≈ 4 characters of content.

        Args:
            messages: Messages to measure.

        Returns:
            Approximate token count.
        """
        return sum(len(str(m.content or "")) for m in messages) // 4

    def _should_auto_compress(self, messages: list[BaseMessage]) -> bool:
        """Decide whether auto-compaction should fire for the given messages.

        Updates ``_last_estimated`` as a side-effect so ``get_usage_info()``
        always reflects the latest call.

        Args:
            messages: Current message list to evaluate.

        Returns:
            True if compaction should proceed.
        """
        estimated = self._estimate_tokens(messages)
        self._last_estimated = estimated
        threshold = int(self._context_window * self._auto_threshold_pct)
        return self._enabled and estimated > threshold and len(messages) > 8

    # ------------------------------------------------------------------
    # Middleware hooks
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Check context usage and auto-compress old messages if needed.

        Args:
            request: Incoming model request.
            call_next: Function to invoke the next handler in the chain.

        Returns:
            The model response from the downstream handler.
        """
        if hasattr(request, "messages") and request.messages:
            messages = list(request.messages)
            if self._should_auto_compress(messages):
                compressed, event = self.compress_now(messages)
                logger.info(
                    "IntelligentCompaction: compressed %d messages (~%d tokens → ~%d tokens, %.1f%% reduction)",
                    event.messages_before,
                    event.tokens_before,
                    event.tokens_after,
                    event.reduction_pct,
                )
                request = request.override(messages=compressed)
        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of ``wrap_model_call``.

        Args:
            request: Incoming model request.
            call_next: Async function to invoke the next handler in the chain.

        Returns:
            The model response from the downstream handler.
        """
        if hasattr(request, "messages") and request.messages:
            messages = list(request.messages)
            if self._should_auto_compress(messages):
                compressed, event = self.compress_now(messages)
                logger.info(
                    "IntelligentCompaction: compressed %d messages (~%d tokens → ~%d tokens, %.1f%% reduction)",
                    event.messages_before,
                    event.tokens_before,
                    event.tokens_after,
                    event.reduction_pct,
                )
                request = request.override(messages=compressed)
        return await call_next(request)
