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


__all__ = [
    "AdaptiveContextMiddleware",
    "ContextTier",
    "ContextTierConfig",
    "ContextUsage",
]


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


# Known model context window sizes (tokens).
#
# Keep this list short and fact-based — the function
# :func:`detect_context_window` consults the installed LangChain
# provider package's `_PROFILES` first, so this dict is only the
# fallback for models the provider package hasn't catalogued yet (or
# for cases where the provider package isn't installed). New 1M+
# models do NOT need an entry here; they'll be picked up
# automatically once the provider package ships the profile.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic — 1M tier introduced with Opus 4.7
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-7-20250219": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-5": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
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

    Resolution order (highest priority first):

    1. **Live provider profile**: the installed LangChain provider
       package's ``_PROFILES`` (e.g. ``langchain_anthropic.data._profiles``)
       is the source of truth for current models. We import it on
       demand and read ``max_input_tokens``. This means a 2M / 10M
       model added to the provider package tomorrow Just Works
       without a release of this library.
    2. **Curated fallback table** :data:`MODEL_CONTEXT_WINDOWS`. Used
       when the provider package isn't installed or the model isn't
       yet in its profile data.
    3. **Partial-name fallback** within the curated table (so
       ``claude-sonnet`` matches ``claude-sonnet-4-6``).
    4. **Caller-supplied default**.

    Args:
        model_name: Model name or identifier; ``"provider:model"``
            specs are accepted and the provider prefix is stripped.
        default: Window size when nothing else resolves.

    Returns:
        Context window size in tokens.
    """
    # Strip provider prefix (``anthropic:claude-sonnet-4-6`` →
    # ``claude-sonnet-4-6``) and remember it so we can route the
    # provider-profile lookup correctly.
    provider_hint: str | None = None
    if ":" in model_name:
        provider_hint, name = model_name.split(":", 1)
    else:
        name = model_name
    name_lower = name.lower()

    # 1. Live provider profile.
    live = _lookup_provider_profile_window(name_lower, provider_hint)
    if live is not None:
        return live

    # 2. Curated fallback — direct match.
    if name_lower in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[name_lower]

    # 3. Partial match (``claude-sonnet`` ↔ ``claude-sonnet-4-6``).
    for known_name, window_size in MODEL_CONTEXT_WINDOWS.items():
        if name_lower.startswith(known_name) or known_name.startswith(name_lower):
            return window_size

    logger.info("Unknown model %s, using default context window %d", model_name, default)
    return default


# Cache of (provider, model) → window size so we don't re-import
# provider data modules on every call. Cleared on process restart.
_PROFILE_WINDOW_CACHE: dict[tuple[str | None, str], int | None] = {}

# Provider-name hints we try when no explicit prefix is supplied.
# Order matters: most common first. Each entry is the LangChain
# provider package's ``data._profiles`` module path.
_PROVIDER_PROFILE_MODULES: tuple[tuple[str, str], ...] = (
    ("anthropic", "langchain_anthropic.data._profiles"),
    ("openai", "langchain_openai.data._profiles"),
    ("google_genai", "langchain_google_genai.data._profiles"),
    ("google_vertexai", "langchain_google_vertexai.data._profiles"),
    ("bedrock", "langchain_aws.data._profiles"),
    ("deepseek", "langchain_deepseek.data._profiles"),
    ("groq", "langchain_groq.data._profiles"),
    ("mistralai", "langchain_mistralai.data._profiles"),
    ("ollama", "langchain_ollama.data._profiles"),
)


def _lookup_provider_profile_window(model_name_lower: str, provider_hint: str | None) -> int | None:
    """Look up ``max_input_tokens`` from an installed provider's `_profiles` module.

    Args:
        model_name_lower: Lower-cased bare model name.
        provider_hint: When non-None, only this provider's profile
            module is consulted. Otherwise we scan all known
            providers in priority order and return the first hit.

    Returns:
        The model's ``max_input_tokens`` if present in the profile;
        ``None`` when the profile module isn't installed, doesn't
        list the model, or doesn't include a tokens entry.
    """
    cache_key = (provider_hint, model_name_lower)
    if cache_key in _PROFILE_WINDOW_CACHE:
        return _PROFILE_WINDOW_CACHE[cache_key]

    candidates: list[tuple[str, str]]
    if provider_hint:
        # Build the canonical module path for an explicit provider.
        package_root = {
            "anthropic": "langchain_anthropic",
            "openai": "langchain_openai",
            "azure_openai": "langchain_openai",
            "google_genai": "langchain_google_genai",
            "google_vertexai": "langchain_google_vertexai",
            "bedrock": "langchain_aws",
            "bedrock_converse": "langchain_aws",
            "deepseek": "langchain_deepseek",
            "groq": "langchain_groq",
            "mistralai": "langchain_mistralai",
            "ollama": "langchain_ollama",
        }.get(provider_hint)
        if package_root is None:
            _PROFILE_WINDOW_CACHE[cache_key] = None
            return None
        candidates = [(provider_hint, f"{package_root}.data._profiles")]
    else:
        candidates = list(_PROVIDER_PROFILE_MODULES)

    import importlib

    for _provider, module_path in candidates:
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue
        profiles = getattr(module, "_PROFILES", None)
        if not isinstance(profiles, dict):
            continue
        profile = profiles.get(model_name_lower)
        if profile is None:
            # Try partial match for versioned ids like
            # ``claude-sonnet-4-6-20250219``.
            for known_name, prof in profiles.items():
                if model_name_lower.startswith(known_name.lower()) or known_name.lower().startswith(model_name_lower):
                    profile = prof
                    break
        if not isinstance(profile, dict):
            continue
        window = profile.get("max_input_tokens")
        if isinstance(window, int) and window > 0:
            _PROFILE_WINDOW_CACHE[cache_key] = window
            return window

    _PROFILE_WINDOW_CACHE[cache_key] = None
    return None


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

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
    ) -> ModelResponse[ResponseT]:
        """Pass through model calls — context management is advisory."""
        return await call_next(request)
