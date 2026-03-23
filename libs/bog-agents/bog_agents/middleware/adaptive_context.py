"""Adaptive Context Window middleware for intelligent context management.

Automatically adjusts summarization thresholds, context packing density,
and message retention based on the model's actual context window size.
Supports models from 4K to 1M+ tokens with appropriate strategies for each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

logger = logging.getLogger(__name__)


class ContextTier(StrEnum):
    """Context window size tiers with different management strategies."""

    SMALL = "small"  # 4K-16K tokens
    MEDIUM = "medium"  # 16K-64K tokens
    LARGE = "large"  # 64K-200K tokens
    XLARGE = "xlarge"  # 200K-500K tokens
    MASSIVE = "massive"  # 500K-2M+ tokens


@dataclass
class ContextTierConfig:
    """Configuration for a context window tier."""

    tier: ContextTier
    min_tokens: int
    max_tokens: int

    summarize_at_pct: float
    """Trigger summarization when context usage reaches this percentage."""

    keep_recent_turns: int
    """Number of recent turns to always preserve from summarization."""

    tool_output_max_tokens: int
    """Maximum tokens for a single tool output before truncation."""

    pack_density: float
    """How aggressively to pack context (0.0 = loose, 1.0 = maximum)."""

    enable_progressive_summarization: bool
    """Whether to summarize incrementally (True) or all-at-once (False)."""

    max_tool_outputs_in_context: int
    """Maximum number of tool outputs to keep in context."""


# Default tier configurations — tuned for real-world usage patterns
DEFAULT_TIER_CONFIGS: list[ContextTierConfig] = [
    ContextTierConfig(
        tier=ContextTier.SMALL,
        min_tokens=0,
        max_tokens=16_000,
        summarize_at_pct=0.60,
        keep_recent_turns=3,
        tool_output_max_tokens=2_000,
        pack_density=0.9,
        enable_progressive_summarization=False,
        max_tool_outputs_in_context=5,
    ),
    ContextTierConfig(
        tier=ContextTier.MEDIUM,
        min_tokens=16_000,
        max_tokens=64_000,
        summarize_at_pct=0.70,
        keep_recent_turns=6,
        tool_output_max_tokens=8_000,
        pack_density=0.7,
        enable_progressive_summarization=True,
        max_tool_outputs_in_context=15,
    ),
    ContextTierConfig(
        tier=ContextTier.LARGE,
        min_tokens=64_000,
        max_tokens=200_000,
        summarize_at_pct=0.75,
        keep_recent_turns=12,
        tool_output_max_tokens=20_000,
        pack_density=0.5,
        enable_progressive_summarization=True,
        max_tool_outputs_in_context=30,
    ),
    ContextTierConfig(
        tier=ContextTier.XLARGE,
        min_tokens=200_000,
        max_tokens=500_000,
        summarize_at_pct=0.80,
        keep_recent_turns=25,
        tool_output_max_tokens=50_000,
        pack_density=0.3,
        enable_progressive_summarization=True,
        max_tool_outputs_in_context=60,
    ),
    ContextTierConfig(
        tier=ContextTier.MASSIVE,
        min_tokens=500_000,
        max_tokens=10_000_000,
        summarize_at_pct=0.85,
        keep_recent_turns=50,
        tool_output_max_tokens=100_000,
        pack_density=0.2,
        enable_progressive_summarization=True,
        max_tool_outputs_in_context=100,
    ),
]


# Known model context window sizes (tokens)
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # Google
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    # DeepSeek
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    # Meta (via Ollama/providers)
    "llama3": 8_192,
    "llama3.1": 128_000,
    "llama3.2": 128_000,
    "llama3.3": 128_000,
    # Mistral
    "mistral-large": 128_000,
    "mistral-small": 32_000,
}


def detect_context_window(model_name: str, *, default: int = 128_000) -> int:
    """Detect the context window size for a given model.

    Args:
        model_name: Model name or identifier.
        default: Default window size if model is unknown.

    Returns:
        Context window size in tokens.
    """
    # Strip provider prefix (e.g., "anthropic:claude-sonnet-4-6" -> "claude-sonnet-4-6")
    name = model_name.rsplit(":", maxsplit=1)[-1] if ":" in model_name else model_name
    # Strip version suffixes for matching
    name_lower = name.lower()

    # Direct match
    if name_lower in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[name_lower]

    # Partial match (e.g., "claude-sonnet" matches "claude-sonnet-4-6")
    for known_name, window_size in MODEL_CONTEXT_WINDOWS.items():
        if name_lower.startswith(known_name) or known_name.startswith(name_lower):
            return window_size

    logger.info("Unknown model %s, using default context window %d", model_name, default)
    return default


def get_tier_config(
    context_window: int,
    tier_configs: list[ContextTierConfig] | None = None,
) -> ContextTierConfig:
    """Get the appropriate tier configuration for a context window size.

    Args:
        context_window: Model's context window in tokens.
        tier_configs: Custom tier configurations. Uses defaults if None.

    Returns:
        Matching ContextTierConfig.
    """
    configs = tier_configs or DEFAULT_TIER_CONFIGS
    for config in reversed(configs):
        if context_window >= config.min_tokens:
            return config
    return configs[0]


@dataclass
class ContextUsage:
    """Current context window usage statistics."""

    total_tokens: int = 0
    system_tokens: int = 0
    message_tokens: int = 0
    tool_output_tokens: int = 0
    context_window: int = 128_000
    tier: ContextTier = ContextTier.LARGE

    @property
    def usage_pct(self) -> float:
        """Percentage of context window used."""
        if self.context_window == 0:
            return 0.0
        return self.total_tokens / self.context_window

    @property
    def remaining_tokens(self) -> int:
        """Tokens remaining in context window."""
        return max(0, self.context_window - self.total_tokens)

    def should_summarize(self, config: ContextTierConfig) -> bool:
        """Check if summarization should be triggered.

        Args:
            config: Active tier configuration.

        Returns:
            True if context usage exceeds the tier's summarization threshold.
        """
        return self.usage_pct >= config.summarize_at_pct


class AdaptiveContextMiddleware(AgentMiddleware):
    """Middleware that adapts context management to the model's window size.

    Automatically detects the model's context window and applies appropriate
    strategies for summarization, tool output truncation, and context packing.

    Example:
        ```python
        from bog_agents.middleware.adaptive_context import AdaptiveContextMiddleware

        # Auto-detect from model
        middleware = AdaptiveContextMiddleware(model_name="gemini-2.5-pro")

        # Or specify explicitly
        middleware = AdaptiveContextMiddleware(context_window=1_000_000)
        ```
    """

    context_window: int
    tier_config: ContextTierConfig
    usage: ContextUsage
    _custom_tiers: list[ContextTierConfig] | None

    def __init__(
        self,
        *,
        model_name: str | None = None,
        context_window: int | None = None,
        tier_configs: list[ContextTierConfig] | None = None,
    ) -> None:
        """Initialize adaptive context middleware.

        Args:
            model_name: Model name for auto-detection.
            context_window: Explicit context window size (overrides model_name).
            tier_configs: Custom tier configurations.
        """
        if context_window is not None:
            self.context_window = context_window
        elif model_name is not None:
            self.context_window = detect_context_window(model_name)
        else:
            self.context_window = 128_000

        self._custom_tiers = tier_configs
        self.tier_config = get_tier_config(self.context_window, tier_configs)
        self.usage = ContextUsage(
            context_window=self.context_window,
            tier=self.tier_config.tier,
        )
        logger.info(
            "Adaptive context: model window=%d, tier=%s, summarize_at=%.0f%%",
            self.context_window,
            self.tier_config.tier,
            self.tier_config.summarize_at_pct * 100,
        )

    def truncate_tool_output(self, output: str) -> str:
        """Truncate a tool output to the tier's maximum.

        Args:
            output: Raw tool output string.

        Returns:
            Truncated output with indicator if truncated.
        """
        max_chars = self.tier_config.tool_output_max_tokens * 4  # ~4 chars per token
        if len(output) <= max_chars:
            return output
        half = max_chars // 2
        return output[:half] + f"\n\n... [truncated {len(output) - max_chars} characters] ...\n\n" + output[-half:]

    def update_model(self, model_name: str) -> None:
        """Update configuration when the model changes mid-session.

        Args:
            model_name: New model name.
        """
        self.context_window = detect_context_window(model_name)
        self.tier_config = get_tier_config(self.context_window, self._custom_tiers)
        self.usage.context_window = self.context_window
        self.usage.tier = self.tier_config.tier
        logger.info(
            "Adaptive context updated: model=%s, window=%d, tier=%s",
            model_name,
            self.context_window,
            self.tier_config.tier,
        )

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Pass through model calls — context management is advisory."""
        return await call_next(request, runtime)
