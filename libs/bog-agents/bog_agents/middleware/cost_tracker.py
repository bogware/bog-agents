"""Middleware for token cost tracking, display, and budget management.

Feature #34: Cost tracking and display — track and expose token usage costs.
Feature #36: Context usage display — show how much context window is used.
Feature #47: Cost budget mode — set dollar limits and optimize within them.
Feature #8: Effort/thinking levels — control reasoning depth.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

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

BUDGET_REACHED = "budget_reached"
"""`type` of the LangGraph interrupt payload raised when `budget_usd` is hit (ROADMAP #51)."""

BudgetMode = Literal["interrupt", "raise", "warn"]
"""What `CostTrackerMiddleware` does when the budget is exceeded before a model call."""


def parse_budget_resume(value: object) -> float | None:
    """Extract a new budget from a `budget_reached` interrupt resume value.

    Accepts `{"budget_usd": N}` (optionally with `"type": "raise_budget"`), a
    bare number, or a numeric string such as `"12"` / `"$3.50"`. Anything else
    — including a non-positive number — yields `None`, which the middleware
    treats as "not raised" and pauses again.

    Args:
        value: The raw value returned by `interrupt()`.

    Returns:
        The new budget in USD, or `None`.
    """
    if isinstance(value, Mapping):
        value = value.get("budget_usd")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip().lstrip("$").replace(",", ""))
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


# Approximate cost per 1M tokens for common models (input/output). Keys are
# *normalized base ids* (see `_normalize_model_for_pricing`) so a full spec such
# as `anthropic:claude-opus-4-6`, a Bedrock id `us.anthropic.claude-opus-4-6-v1:0`,
# or a dated `claude-opus-4-6-20250101` all resolve to the same row. Longest key
# wins on a prefix match, so `claude-opus` can't shadow `claude-opus-4-6`.
_MODEL_COSTS: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-haiku-4-5": (0.80, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-8": (15.0, 75.0),
    # Family fallbacks (shorter keys, matched only when a specific row doesn't).
    "claude-haiku": (0.80, 4.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-opus": (15.0, 75.0),
    # OpenAI
    "gpt-5": (10.0, 30.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "o4-mini": (1.10, 4.40),
    "o3": (10.0, 40.0),
    # Google
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-3-flash": (0.15, 0.60),
    "gemini-3-pro": (2.50, 15.0),
    # DeepSeek
    "deepseek-r1": (0.55, 2.19),
    "deepseek-v3": (0.27, 1.10),
}

# Cost assumed for a model we can't price. The normalized lookup resolves the
# common prefixed/suffixed ids now, so this default is hit far less often than
# under the old exact-match scheme that mis-billed 3-5x on Opus/Bedrock (CTX-3).
_DEFAULT_MODEL_COST: tuple[float, float] = (5.0, 15.0)

# Provider/route prefixes stripped before matching: `anthropic:`, `openai:`,
# `bedrock:`, `openrouter/`, `us.anthropic.`, `eu.meta.`, `us-gov.amazon.`, …
_PROVIDER_PREFIX_RE = re.compile(r"^(?:[a-z0-9-]+[:/])+")
_BEDROCK_REGION_RE = re.compile(r"^(?:us|eu|apac|us-gov|ap|ca|sa)[.-](?:anthropic|meta|amazon|cohere|mistral|ai21)\.")
_VENDOR_DOT_RE = re.compile(r"^(?:anthropic|meta|amazon|cohere|mistral|ai21|google)\.")
_BEDROCK_VERSION_RE = re.compile(r"[-:]v\d+(?::\d+)?$")
_DATE_SUFFIX_RE = re.compile(r"-\d{6,8}$")
_PRICED_KEYS_BY_LEN: list[str] = sorted(_MODEL_COSTS, key=len, reverse=True)


def _normalize_model_for_pricing(name: str) -> str:
    """Reduce a full model spec to a base id for pricing lookup.

    Handles the id shapes CTX-3 broke on: provider prefixes (`anthropic:`,
    `openrouter/`), Bedrock region+vendor prefixes and `-v1:0` version suffixes,
    and trailing date stamps. Best-effort and lossless-enough for pricing — it
    never raises.
    """
    if not name:
        return ""
    s = name.strip().lower()
    # Order matters: strip the Bedrock `-v1:0` version suffix FIRST, otherwise the
    # generic `word:`/`word/` provider-prefix rule greedily eats the `v1:` and
    # leaves just `0`. Then region+vendor prefix, then provider prefix, then date.
    s = _BEDROCK_VERSION_RE.sub("", s)
    s = _BEDROCK_REGION_RE.sub("", s)
    s = _PROVIDER_PREFIX_RE.sub("", s)
    s = _VENDOR_DOT_RE.sub("", s)
    s = _DATE_SUFFIX_RE.sub("", s)
    return s


def price_for_model(name: str) -> tuple[float, float] | None:
    """Return (input, output) $/1M tokens for `name`, or None when unpriced.

    Tries an exact normalized match, then the longest catalog key that is a
    prefix of (or contained in) the normalized id, so `claude-opus-4-6` beats
    the `claude-opus` family fallback.
    """
    norm = _normalize_model_for_pricing(name)
    if not norm:
        return None
    if norm in _MODEL_COSTS:
        return _MODEL_COSTS[norm]
    for key in _PRICED_KEYS_BY_LEN:
        if norm.startswith(key) or key in norm:
            return _MODEL_COSTS[key]
    return None


# Default context window sizes.
#
# Curated fallback only — :func:`_resolve_context_window` consults the
# installed LangChain provider package's ``_PROFILES`` first via
# :func:`bog_agents.middleware.adaptive_context.detect_context_window`,
# so new 1M+ models do not require an entry here. Keep the table
# short and update only when a model is missing upstream.
_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic — 1M tier with Opus 4.7
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    # OpenAI
    "gpt-5": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    # Google
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


__all__ = [
    "CostTracker",
    "CostTrackerMiddleware",
    "UsageSnapshot",
]


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
    # P1-5: hard cap on the in-memory snapshot list. A long session
    # (hours, hundreds of turns) used to accumulate one record per
    # ``record_usage`` indefinitely — at ~250 bytes each the structure
    # alone tipped past 1MB on extended sessions. The current totals
    # live on the parent fields; the snapshot list is for fine-grained
    # charts, so capping it doesn't lose the headline numbers. Override
    # via ``max_snapshots`` if you need more granularity.
    max_snapshots: int = 1000
    # Guards record_usage so token totals stay coherent under concurrent
    # turns (parallel-worktree, multi-agent). Held only across the integer
    # increments + snapshot append, never across model I/O.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        """Estimated cost in USD based on normalized model pricing (CTX-3)."""
        costs = price_for_model(self.model_name) or _DEFAULT_MODEL_COST
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
        """Get the context window size for the current model.

        Routes through :func:`adaptive_context.detect_context_window`
        so live LangChain provider profiles win over the curated
        fallback. Without this routing, ``cost_tracker`` would silently
        cap a 1M-context model at the legacy 200K default and the
        in-session budget bar would lie about how much headroom is
        left.
        """
        from bog_agents.middleware.adaptive_context import detect_context_window

        # Pass our own dict as a last-resort hint by raising the
        # default to whatever the curated table has for this model
        # name, so callers still get the curated value when the
        # provider package isn't installed.
        fallback = _CONTEXT_WINDOWS.get(self.model_name, 200_000)
        return detect_context_window(self.model_name, default=fallback)

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
        with self._lock:
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
            # P1-5: drop the oldest snapshots in bulk once the cap is
            # exceeded, so the slice happens at most once per N inserts.
            if len(self.snapshots) > self.max_snapshots:
                overflow = len(self.snapshots) - self.max_snapshots
                del self.snapshots[:overflow]

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

        if pct > 80:
            lines.append("WARNING: Context window is getting full. Consider using /compact.")
        elif pct > 60:
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
        strict_budget: bool = True,
        on_budget: BudgetMode | None = None,
        interrupt_fn: Callable[[Any], Any] | None = None,
    ) -> None:
        """Create the middleware.

        Args:
            model_name: Model id used for pricing.
            budget_usd: Optional hard cap in USD; `None` is unlimited.
            effort_level: Initial effort level exposed through `set_effort`.
            strict_budget: Legacy switch: `False` means `on_budget="warn"`.
            on_budget: What happens when the cap is hit before a model call
                (ROADMAP #51). `"interrupt"` (default) pauses the graph with a
                `budget_reached` interrupt that only a raise-cap resume clears
                (see `parse_budget_resume`); outside a checkpointed graph it
                falls back to `"raise"`. `"raise"` ends the turn with
                `RuntimeError` (the pre-#51 behaviour); `"warn"` logs and
                continues.
            interrupt_fn: Injectable replacement for `langgraph.types.interrupt`
                (tests); resolved lazily when `None`.
        """
        self.tracker = CostTracker(model_name=model_name, budget_usd=budget_usd)
        self._effort_level = effort_level
        self._strict_budget = strict_budget
        self._on_budget: BudgetMode = on_budget if on_budget is not None else ("interrupt" if strict_budget else "warn")
        self._interrupt_fn = interrupt_fn
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
        self._apply_context_budget(request)
        self._enforce_budget()

        response = call_next(request)
        self._record_usage_from_response(response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        self._apply_context_budget(request)
        self._enforce_budget()

        response = await call_next(request)
        self._record_usage_from_response(response)
        return response

    # ------------------------------------------------------------------ budget (#51)

    def _apply_context_budget(self, request: ModelRequest) -> None:
        """Honour a per-turn `budget_usd` carried on `runtime.context`.

        The CLI's agent lives in a separate server process, so `/cost budget N`
        cannot reach `set_budget` in-process; the TUI carries the choice on the
        per-turn context instead (the `ThinkingMiddleware` pattern). A value of
        `0` (or less) lifts the cap; `None` / absent keeps the current one.

        Args:
            request: The current model request.
        """
        runtime = getattr(request, "runtime", None)
        ctx = getattr(runtime, "context", None)
        if ctx is None:
            return
        value = ctx.get("budget_usd") if isinstance(ctx, Mapping) else getattr(ctx, "budget_usd", None)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        self.tracker.budget_usd = float(value) if value > 0 else None

    def _budget_message(self) -> str:
        """Render the budget-exceeded message."""
        return (
            f"Cost budget exceeded: ${self.tracker.estimated_cost_usd:.4f} spent "
            f"of ${self.tracker.budget_usd or 0:.2f} budget. "
            f"Increase budget_usd or start a new session."
        )

    def budget_payload(self) -> dict[str, Any]:
        """Build the `budget_reached` interrupt payload for the current spend."""
        return {
            "type": BUDGET_REACHED,
            "spent_usd": round(self.tracker.estimated_cost_usd, 6),
            "budget_usd": self.tracker.budget_usd,
            "model": self.tracker.model_name,
            "message": self._budget_message(),
            "resume": "Resume with {'budget_usd': <new cap>} to raise the budget and continue; anything else pauses again.",
        }

    def _enforce_budget(self) -> None:
        """Gate the next model call on the budget (ROADMAP #51).

        Raises:
            RuntimeError: In `raise` mode, or in `interrupt` mode when no
                checkpointed graph is available to hold the pause.
        """
        if not self.tracker.budget_exceeded:
            return
        if self._on_budget == "warn":
            logger.warning(self._budget_message())
            return
        if self._on_budget == "raise":
            raise RuntimeError(self._budget_message())
        interrupt_fn = self._interrupt_fn
        if interrupt_fn is None:
            from langgraph.types import interrupt

            interrupt_fn = interrupt
        while self.tracker.budget_exceeded:
            try:
                resume = interrupt_fn(self.budget_payload())
            except RuntimeError as exc:
                # No checkpointer / not inside a graph: the pause has nowhere to
                # live, so fall back to the pre-#51 hard stop.
                raise RuntimeError(self._budget_message()) from exc
            new_cap = parse_budget_resume(resume)
            if new_cap is not None and new_cap > self.tracker.estimated_cost_usd:
                self.tracker.budget_usd = new_cap
                logger.info("Budget raised to $%.2f after a budget_reached pause", new_cap)

    def _record_usage_from_response(self, response: ModelResponse) -> None:
        """Record token usage from a ModelResponse.

        langchain's ``ModelResponse`` exposes ``.result`` (a list of messages),
        NOT ``.response_metadata``. Token usage lives on the AIMessage's
        standardized ``usage_metadata`` (``input_tokens``/``output_tokens`` plus
        ``input_token_details`` for cache), with a fallback to the raw
        ``response_metadata`` usage dict for providers that don't populate the
        standard field.
        """
        messages = getattr(response, "result", None)
        if not isinstance(messages, list):
            return
        ai = next(
            (m for m in reversed(messages) if getattr(m, "type", "") == "ai" or m.__class__.__name__ == "AIMessage"),
            None,
        )
        if ai is None:
            return

        usage = getattr(ai, "usage_metadata", None)
        if isinstance(usage, dict) and usage:
            details = usage.get("input_token_details") or {}
            self.tracker.record_usage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read=int(details.get("cache_read", 0) or 0),
                cache_write=int(details.get("cache_creation", 0) or 0),
            )
            return

        # Fallback: raw provider response_metadata.
        metadata = getattr(ai, "response_metadata", {}) or {}
        raw = metadata.get("usage", {}) or metadata.get("token_usage", {})
        if raw:
            self.tracker.record_usage(
                input_tokens=int(raw.get("input_tokens", 0) or raw.get("prompt_tokens", 0) or 0),
                output_tokens=int(raw.get("output_tokens", 0) or raw.get("completion_tokens", 0) or 0),
                cache_read=int(raw.get("cache_read_input_tokens", 0) or 0),
                cache_write=int(raw.get("cache_creation_input_tokens", 0) or 0),
            )
