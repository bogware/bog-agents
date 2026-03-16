"""Middleware for token cost tracking, display, and budget management.

Feature #34: Cost tracking and display — track and expose token usage costs.
Feature #36: Context usage display — show how much context window is used.
Feature #47: Cost budget mode — set dollar limits and optimize within them.
Feature #8: Effort/thinking levels — control reasoning depth.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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

logger = logging.getLogger(__name__)

# Approximate cost per 1M tokens for common models (input/output)
_MODEL_COSTS: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-haiku-4-5": (0.80, 4.0),
    # OpenAI
    "gpt-5": (10.0, 30.0),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (10.0, 40.0),
    "o4-mini": (1.10, 4.40),
    # Google
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-3-flash": (0.15, 0.60),
    "gemini-3-pro": (2.50, 15.0),
    # DeepSeek
    "deepseek-r1": (0.55, 2.19),
    "deepseek-v3": (0.27, 1.10),
}

# Default context window sizes
_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "gpt-5": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-3-flash": 1_000_000,
    "gemini-3-pro": 1_000_000,
}

# Effort level configurations
EFFORT_LEVELS: dict[str, dict[str, Any]] = {
    "low": {
        "description": "Quick responses, minimal reasoning",
        "max_tokens": 1024,
        "temperature": 0.3,
    },
    "medium": {
        "description": "Balanced reasoning and speed (default)",
        "max_tokens": 4096,
        "temperature": 0.5,
    },
    "high": {
        "description": "Thorough reasoning and analysis",
        "max_tokens": 8192,
        "temperature": 0.7,
    },
    "max": {
        "description": "Maximum reasoning depth, extended thinking",
        "max_tokens": 16384,
        "temperature": 1.0,
    },
}


@dataclass
class UsageSnapshot:
    """Token usage snapshot at a point in time."""

    timestamp: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


@dataclass
class CostTracker:
    """Tracks cumulative token usage and costs across a session."""

    model_name: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_requests: int = 0
    session_start: float = field(default_factory=time.time)
    budget_usd: float | None = None
    snapshots: list[UsageSnapshot] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        """Estimated cost in USD based on model pricing."""
        costs = _MODEL_COSTS.get(self.model_name, (5.0, 15.0))
        input_cost = (self.input_tokens / 1_000_000) * costs[0]
        output_cost = (self.output_tokens / 1_000_000) * costs[1]
        return input_cost + output_cost

    @property
    def budget_remaining_usd(self) -> float | None:
        """Remaining budget in USD, or None if no budget set."""
        if self.budget_usd is None:
            return None
        return max(0, self.budget_usd - self.estimated_cost_usd)

    @property
    def budget_exceeded(self) -> bool:
        """Whether the cost budget has been exceeded."""
        if self.budget_usd is None:
            return False
        return self.estimated_cost_usd >= self.budget_usd

    @property
    def context_window_size(self) -> int:
        """Get the context window size for the current model."""
        return _CONTEXT_WINDOWS.get(self.model_name, 200_000)

    def record_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        """Record token usage from a model call.

        Args:
            input_tokens: Input tokens used.
            output_tokens: Output tokens generated.
            cache_read: Cache read tokens.
            cache_write: Cache write tokens.
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.total_requests += 1
        self.snapshots.append(
            UsageSnapshot(
                timestamp=time.time(),
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cache_read_tokens=self.cache_read_tokens,
                cache_write_tokens=self.cache_write_tokens,
                total_tokens=self.total_tokens,
                estimated_cost_usd=self.estimated_cost_usd,
            )
        )

    def format_summary(self) -> str:
        """Format a human-readable cost summary.

        Returns:
            Formatted cost summary string.
        """
        elapsed = time.time() - self.session_start
        minutes = elapsed / 60

        lines = [
            "## Token Usage Summary",
            f"Model: {self.model_name}",
            f"Requests: {self.total_requests}",
            f"Session duration: {minutes:.1f} minutes",
            "",
            "### Tokens",
            f"  Input:  {self.input_tokens:>10,}",
            f"  Output: {self.output_tokens:>10,}",
            f"  Total:  {self.total_tokens:>10,}",
        ]

        if self.cache_read_tokens or self.cache_write_tokens:
            lines.extend(
                [
                    "",
                    "### Cache",
                    f"  Read:  {self.cache_read_tokens:>10,}",
                    f"  Write: {self.cache_write_tokens:>10,}",
                ]
            )

        lines.extend(
            [
                "",
                f"### Estimated Cost: ${self.estimated_cost_usd:.4f}",
            ]
        )

        if self.budget_usd is not None:
            remaining = self.budget_remaining_usd or 0
            pct = (self.estimated_cost_usd / self.budget_usd * 100) if self.budget_usd > 0 else 0
            lines.extend(
                [
                    f"Budget: ${self.budget_usd:.2f}",
                    f"Used: {pct:.1f}%",
                    f"Remaining: ${remaining:.4f}",
                ]
            )
            if self.budget_exceeded:
                lines.append("WARNING: Budget exceeded!")

        return "\n".join(lines)

    def format_context_usage(self, current_tokens: int = 0) -> str:
        """Format context window usage information.

        Args:
            current_tokens: Current tokens in the context window.

        Returns:
            Formatted context usage string.
        """
        window = self.context_window_size
        pct = (current_tokens / window * 100) if window > 0 else 0
        remaining = max(0, window - current_tokens)

        lines = [
            "## Context Window Usage",
            f"Model: {self.model_name}",
            f"Window size: {window:,} tokens",
            f"Current usage: {current_tokens:,} tokens ({pct:.1f}%)",
            f"Remaining: {remaining:,} tokens",
        ]

        if pct > 80:  # noqa: PLR2004
            lines.append("WARNING: Context window is getting full. Consider using /compact.")
        elif pct > 60:  # noqa: PLR2004
            lines.append("Note: Over 60% of context used. Auto-compaction may trigger soon.")

        return "\n".join(lines)


class CostTrackerState(TypedDict):
    """State for cost tracker middleware."""


class CostTrackerMiddleware(AgentMiddleware[CostTrackerState, ContextT, ResponseT]):
    """Middleware for tracking token costs, context usage, and budget enforcement.

    Provides tools for:
    - Viewing current token usage and costs (/cost, /tokens)
    - Viewing context window usage (/context)
    - Setting and enforcing cost budgets
    - Adjusting effort/thinking levels

    Args:
        model_name: Name of the model for cost estimation.
        budget_usd: Optional cost budget in USD.
        effort_level: Initial effort level (low/medium/high/max).
    """

    state_schema = CostTrackerState

    def __init__(
        self,
        *,
        model_name: str = "",
        budget_usd: float | None = None,
        effort_level: str = "medium",
    ) -> None:
        self.tracker = CostTracker(model_name=model_name, budget_usd=budget_usd)
        self._effort_level = effort_level
        self.tools = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build cost tracking tools."""
        middleware = self

        def show_cost(
            runtime: ToolRuntime[None, CostTrackerState],
        ) -> str:
            """Show current token usage and estimated costs for this session."""
            return middleware.tracker.format_summary()

        def show_context(
            runtime: ToolRuntime[None, CostTrackerState],
            current_tokens: int = 0,
        ) -> str:
            """Show context window usage. Pass current_tokens for accurate measurement."""
            return middleware.tracker.format_context_usage(current_tokens)

        def set_budget(
            runtime: ToolRuntime[None, CostTrackerState],
            budget_usd: float = 0,
        ) -> str:
            """Set a cost budget for this session in USD. Use 0 to remove budget."""
            if budget_usd <= 0:
                middleware.tracker.budget_usd = None
                return "Budget removed. No cost limit enforced."
            middleware.tracker.budget_usd = budget_usd
            return f"Budget set to ${budget_usd:.2f}. Current spend: ${middleware.tracker.estimated_cost_usd:.4f}"

        def set_effort(
            runtime: ToolRuntime[None, CostTrackerState],
            level: str = "medium",
        ) -> str:
            """Set the effort/thinking level: 'low', 'medium', 'high', or 'max'."""
            if level not in EFFORT_LEVELS:
                return f"Invalid effort level '{level}'. Use: {', '.join(EFFORT_LEVELS.keys())}"
            middleware._effort_level = level
            config = EFFORT_LEVELS[level]
            return f"Effort set to '{level}': {config['description']}"

        return [
            StructuredTool.from_function(name="show_cost", description="Show token usage and estimated costs.", func=show_cost),
            StructuredTool.from_function(name="show_context", description="Show context window usage.", func=show_context),
            StructuredTool.from_function(name="set_budget", description="Set a cost budget in USD.", func=set_budget),
            StructuredTool.from_function(name="set_effort", description="Set effort/thinking level.", func=set_effort),
        ]

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Track token usage from model calls."""
        # Check budget before calling
        if self.tracker.budget_exceeded:
            logger.warning("Cost budget exceeded: $%.4f / $%.2f", self.tracker.estimated_cost_usd, self.tracker.budget_usd)

        response = call_next(request)

        # Extract usage from response if available
        if hasattr(response, "response_metadata"):
            metadata = getattr(response, "response_metadata", {})
            usage = metadata.get("usage", {}) or metadata.get("token_usage", {})
            if usage:
                self.tracker.record_usage(
                    input_tokens=usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
                    cache_read=usage.get("cache_read_input_tokens", 0),
                    cache_write=usage.get("cache_creation_input_tokens", 0),
                )

        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        if self.tracker.budget_exceeded:
            logger.warning("Cost budget exceeded: $%.4f / $%.2f", self.tracker.estimated_cost_usd, self.tracker.budget_usd)

        response = await call_next(request)

        if hasattr(response, "response_metadata"):
            metadata = getattr(response, "response_metadata", {})
            usage = metadata.get("usage", {}) or metadata.get("token_usage", {})
            if usage:
                self.tracker.record_usage(
                    input_tokens=usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
                    cache_read=usage.get("cache_read_input_tokens", 0),
                    cache_write=usage.get("cache_creation_input_tokens", 0),
                )

        return response
