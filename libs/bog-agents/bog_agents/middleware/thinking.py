"""Extended Thinking middleware — enables deep reasoning on supported models.

Activates the model's extended thinking / reasoning mode so it reasons
step-by-step before producing its final answer. This yields measurably
better results on complex architectural, debugging, and planning tasks.

Supported providers
-------------------
- **Anthropic** (claude-3-7-sonnet-20250219+, claude-opus-4-7+):
  Uses ``thinking={"type": "enabled", "budget_tokens": N}`` in the API call.
- **Google Gemini** (gemini-2.5-pro and later):
  Uses ``generation_config={"thinking_config": {"thinking_budget": N}}``.
- **OpenAI o-series** (o1, o3, o4-mini):
  Uses ``reasoning_effort="high"`` (models already think internally).
- **Fallback**: Injects a "think step by step" system-prompt instruction for
  models that don't support native thinking APIs.

Usage::

    from bog_agents.middleware.thinking import ThinkingMiddleware

    agent = create_agent(
        model="claude-opus-4-7",
        middleware=[ThinkingMiddleware(enabled=True, budget_tokens=10000)],
    )

Toggle at runtime via the ``/think`` CLI command::

    /think               # toggle on/off
    /think --budget 5000 # enable with custom token budget
    /think off           # disable

The middleware exposes a ``set_thinking(enabled, budget_tokens)`` method
that the CLI can call after creation to change state mid-session.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.tools import BaseTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Default token budget for extended thinking
_DEFAULT_BUDGET = 8_000
_FALLBACK_PROMPT = (
    "Before answering, think through this step-by-step in a <thinking> block. "
    "Reason carefully about the problem, consider edge cases, and only then "
    "provide your final answer."
)


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------


def _detect_provider(model_name: str) -> str:
    """Detect the AI provider from a model name string.

    Args:
        model_name: Model identifier string.

    Returns:
        Provider tag: 'anthropic', 'google', 'openai', or 'unknown'.
    """
    name = model_name.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gemini", "models/gemini")):
        return "google"
    if name.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return "unknown"


def _model_supports_native_thinking(model_name: str) -> bool:
    """Return True if the model supports the native thinking/reasoning API.

    Args:
        model_name: Model identifier string.

    Returns:
        True if native thinking is supported.
    """
    name = model_name.lower()
    # Anthropic: claude-3-7-sonnet and later, claude-opus-4+
    if "claude-3-7" in name or "claude-3.7" in name:
        return True
    if "claude-sonnet-4" in name or "claude-opus-4" in name or "claude-haiku-4" in name:
        return True
    if "gemini-2.5" in name or "gemini-2-5" in name:
        return True
    # OpenAI o-series think internally (we just set reasoning_effort)
    if name.startswith(("o1", "o3", "o4")):
        return True
    return False


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ThinkingConfig:
    """Configuration for extended thinking.

    Attributes:
        enabled: Whether extended thinking is active.
        budget_tokens: Token budget for thinking (Anthropic/Gemini).
    """

    enabled: bool = False
    budget_tokens: int = _DEFAULT_BUDGET


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class ThinkingState(TypedDict):
    """LangGraph state for the thinking middleware."""


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ThinkingMiddleware(AgentMiddleware[ThinkingState, ContextT, ResponseT]):
    """Enable extended thinking / deep reasoning on the active model.

    When enabled, this middleware injects the appropriate thinking parameters
    for the model's provider: native API parameters where supported, or a
    chain-of-thought system-prompt injection as fallback.

    Args:
        enabled: Start with thinking enabled.
        budget_tokens: Token budget for thinking steps (default 8 000).
        fallback_prompt: Custom chain-of-thought instruction for models that
            don't support native thinking APIs.
    """

    state_schema = ThinkingState

    def __init__(
        self,
        *,
        enabled: bool = False,
        budget_tokens: int = _DEFAULT_BUDGET,
        fallback_prompt: str = _FALLBACK_PROMPT,
    ) -> None:
        self._config = ThinkingConfig(enabled=enabled, budget_tokens=budget_tokens)
        self._fallback_prompt = fallback_prompt
        self._tools: list[BaseTool] = []

    # ------------------------------------------------------------------
    # Public API (called by /think CLI command)
    # ------------------------------------------------------------------

    def set_thinking(self, enabled: bool, *, budget_tokens: int | None = None) -> None:
        """Enable or disable extended thinking, optionally updating the budget.

        Args:
            enabled: Whether to enable thinking.
            budget_tokens: New token budget (if provided).
        """
        self._config.enabled = enabled
        if budget_tokens is not None:
            self._config.budget_tokens = max(1_000, budget_tokens)

    def toggle(self) -> bool:
        """Toggle thinking on/off.

        Returns:
            The new enabled state.
        """
        self._config.enabled = not self._config.enabled
        return self._config.enabled

    @property
    def is_enabled(self) -> bool:
        """Return True if thinking is currently enabled."""
        return self._config.enabled

    @property
    def budget_tokens(self) -> int:
        """Return the current token budget."""
        return self._config.budget_tokens

    @property
    def tools(self) -> list[BaseTool]:
        """No tools exposed — thinking is controlled via middleware config."""
        return self._tools

    # ------------------------------------------------------------------
    # Model binding helpers
    # ------------------------------------------------------------------

    def _bind_thinking_params(self, model: Any, model_name: str) -> Any:
        """Bind thinking parameters to a LangChain chat model.

        Args:
            model: LangChain chat model instance.
            model_name: Model name string for provider detection.

        Returns:
            Model with thinking parameters bound, or original model.
        """
        provider = _detect_provider(model_name)
        budget = self._config.budget_tokens

        try:
            if provider == "anthropic":
                return model.bind(
                    thinking={"type": "enabled", "budget_tokens": budget}
                )
            if provider == "google":
                return model.bind(
                    generation_config={
                        "thinking_config": {"thinking_budget": budget, "include_thoughts": True}
                    }
                )
            if provider == "openai":
                # o-series models use reasoning_effort
                return model.bind(reasoning_effort="high")
        except Exception as exc:
            logger.debug("Failed to bind thinking params to model: %s", exc)
        return model

    def _get_model_name(self, request: ModelRequest) -> str:
        """Extract the model name from a ModelRequest.

        Args:
            request: The model request object.

        Returns:
            Model name string, or empty string if not found.
        """
        # Try common attributes
        for attr in ("model_name", "model", "_model_name"):
            val = getattr(request, attr, None)
            if isinstance(val, str):
                return val
        return ""

    # ------------------------------------------------------------------
    # Middleware hooks
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Apply thinking configuration for sync model calls.

        Args:
            request: Current model request.
            call_next: Next middleware or model call.

        Returns:
            Model response.
        """
        if not self._config.enabled:
            return call_next(request)

        model_name = self._get_model_name(request)

        if _model_supports_native_thinking(model_name):
            # Attempt to bind native thinking params to the model in the request
            if hasattr(request, "model") and request.model is not None:
                request = request.model_copy(
                    update={"model": self._bind_thinking_params(request.model, model_name)}
                )
        else:
            # Fallback: chain-of-thought system prompt injection
            request = append_to_system_message(request, self._fallback_prompt)

        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Apply thinking configuration for async model calls.

        Args:
            request: Current model request.
            call_next: Next middleware or model call.

        Returns:
            Model response.
        """
        if not self._config.enabled:
            return await call_next(request)

        model_name = self._get_model_name(request)

        if _model_supports_native_thinking(model_name):
            if hasattr(request, "model") and request.model is not None:
                request = request.model_copy(
                    update={"model": self._bind_thinking_params(request.model, model_name)}
                )
        else:
            request = append_to_system_message(request, self._fallback_prompt)

        return await call_next(request)
