"""Model Cascading middleware for intelligent cost-aware model routing.

Routes tasks to the cheapest model capable of handling them. Simple tasks
go to fast/cheap models, complex tasks go to frontier models. Learns from
task outcomes to improve routing over time.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


def _latest_human_text(request: ModelRequest[Any]) -> str | None:
    """Extract the plain text of the most recent human message in a request.

    Walks `request.messages` from the end and returns the first
    `HumanMessage`'s text content, flattening list-shaped content into a
    single string. Returns None when no human message is present or it has
    no extractable text.

    Args:
        request: The pending model request.

    Returns:
        The latest human message text, or None when unavailable.
    """
    messages = getattr(request, "messages", None) or []
    for msg in reversed(list(messages)):
        if not isinstance(msg, HumanMessage):
            continue
        content = msg.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    value = block.get("text")
                    if isinstance(value, str):
                        parts.append(value)
            text = " ".join(parts)
        else:
            text = str(content)
        text = text.strip()
        return text or None
    return None


class TaskComplexity(StrEnum):
    """Complexity classification for tasks."""

    TRIVIAL = "trivial"  # Simple questions, file reads
    SIMPLE = "simple"  # Single-file edits, basic searches
    MODERATE = "moderate"  # Multi-file changes, test writing
    COMPLEX = "complex"  # Architecture changes, debugging
    EXPERT = "expert"  # System design, complex refactoring


@dataclass
class ModelTier:
    """A model tier in the cascade with cost and capability info."""

    name: str
    """Human-readable tier name (e.g., 'fast', 'standard', 'frontier')."""

    model_id: str
    """LangChain model identifier (e.g., 'anthropic:claude-haiku-4-5')."""

    cost_per_1k_input: float
    """Cost per 1K input tokens in USD."""

    cost_per_1k_output: float
    """Cost per 1K output tokens in USD."""

    max_complexity: TaskComplexity
    """Maximum task complexity this tier can handle well."""

    context_window: int = 128_000
    """Context window size in tokens."""

    supports_tools: bool = True
    """Whether this model supports tool calling."""

    supports_vision: bool = False
    """Whether this model supports image inputs."""


# Default model cascade tiers
DEFAULT_CASCADE: list[ModelTier] = [
    ModelTier(
        name="fast",
        model_id="anthropic:claude-haiku-4-5",
        cost_per_1k_input=0.0008,
        cost_per_1k_output=0.004,
        max_complexity=TaskComplexity.SIMPLE,
        context_window=200_000,
    ),
    ModelTier(
        name="standard",
        model_id="anthropic:claude-sonnet-4-6",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        max_complexity=TaskComplexity.COMPLEX,
        context_window=200_000,
        supports_vision=True,
    ),
    ModelTier(
        name="frontier",
        model_id="anthropic:claude-opus-4-6",
        cost_per_1k_input=0.015,
        cost_per_1k_output=0.075,
        max_complexity=TaskComplexity.EXPERT,
        context_window=200_000,
        supports_vision=True,
    ),
]


# Complexity signals — patterns in user messages that indicate task complexity
COMPLEXITY_SIGNALS: dict[TaskComplexity, list[str]] = {
    TaskComplexity.TRIVIAL: [
        r"\b(what is|show me|list|print|display|read)\b",
        r"\b(version|help|status)\b",
    ],
    TaskComplexity.SIMPLE: [
        r"\b(rename|move|copy|delete|add comment|fix typo)\b",
        r"\b(change|update|modify)\b.*\b(one|single|this)\b",
    ],
    TaskComplexity.MODERATE: [
        r"\b(refactor|test|implement|create)\b",
        r"\b(multiple files|several|across)\b",
        r"\b(add feature|new feature)\b",
    ],
    TaskComplexity.COMPLEX: [
        r"\b(architecture|design|debug|investigate)\b",
        r"\b(performance|optimize|security audit)\b",
        r"\b(migrate|upgrade|rewrite)\b",
    ],
    TaskComplexity.EXPERT: [
        r"\b(system design|distributed|concurrent)\b",
        r"\b(from scratch|ground up|entire)\b.*\b(system|application)\b",
    ],
}


@dataclass
class RoutingDecision:
    """A record of a model routing decision."""

    task_summary: str
    complexity: TaskComplexity
    selected_tier: str
    model_id: str
    timestamp: float = field(default_factory=time.time)
    success: bool | None = None
    fallback_used: bool = False


@dataclass
class CascadeHistory:
    """Tracks routing decisions for learning."""

    decisions: list[RoutingDecision] = field(default_factory=list)

    def record(self, decision: RoutingDecision) -> None:
        """Record a routing decision."""
        self.decisions.append(decision)

    def tier_success_rate(self, tier_name: str, complexity: TaskComplexity) -> float:
        """Get the success rate for a tier at a given complexity.

        Args:
            tier_name: Model tier name.
            complexity: Task complexity level.

        Returns:
            Success ratio (0.0-1.0), or 0.5 if no data.
        """
        relevant = [d for d in self.decisions if d.selected_tier == tier_name and d.complexity == complexity and d.success is not None]
        if not relevant:
            return 1.0  # No data — assume the tier works
        return sum(1 for d in relevant if d.success) / len(relevant)

    @property
    def total_decisions(self) -> int:
        """Total number of routing decisions made."""
        return len(self.decisions)


def classify_complexity(
    message: str,
    *,
    tool_count: int = 0,
    turn_count: int = 0,
) -> TaskComplexity:
    """Classify the complexity of a task from the user message.

    Args:
        message: The user's message/task description.
        tool_count: Number of tool calls in the current session.
        turn_count: Number of conversation turns so far.

    Returns:
        Estimated TaskComplexity.
    """
    message_lower = message.lower()
    scores: dict[TaskComplexity, float] = dict.fromkeys(TaskComplexity, 0.0)

    # Pattern matching
    for complexity, patterns in COMPLEXITY_SIGNALS.items():
        for pattern in patterns:
            if re.search(pattern, message_lower):
                scores[complexity] += 1.0

    # Message length heuristic
    word_count = len(message.split())
    if word_count > 200:
        scores[TaskComplexity.COMPLEX] += 0.5
    elif word_count > 100:
        scores[TaskComplexity.MODERATE] += 0.5
    elif word_count < 20:
        scores[TaskComplexity.TRIVIAL] += 0.5

    # Session context heuristic
    if tool_count > 20:
        scores[TaskComplexity.COMPLEX] += 0.5
    if turn_count > 15:
        scores[TaskComplexity.MODERATE] += 0.3

    # Find highest scoring complexity
    best = max(scores, key=lambda k: scores[k])

    # If no clear signal, default to MODERATE
    if scores[best] == 0.0:
        return TaskComplexity.MODERATE

    return best


def select_model_tier(
    complexity: TaskComplexity,
    cascade: list[ModelTier],
    history: CascadeHistory,
    *,
    require_vision: bool = False,
    require_tools: bool = True,
    min_context: int = 0,
) -> ModelTier:
    """Select the cheapest adequate model tier for a task.

    Args:
        complexity: Classified task complexity.
        cascade: Available model tiers, ordered by cost.
        history: Historical routing data for learning.
        require_vision: Whether the task needs vision capability.
        require_tools: Whether the task needs tool calling.
        min_context: Minimum context window required.

    Returns:
        Selected ModelTier.
    """
    complexity_levels = list(TaskComplexity)
    complexity_idx = complexity_levels.index(complexity)

    for tier in cascade:
        # Check capability requirements
        if require_vision and not tier.supports_vision:
            continue
        if require_tools and not tier.supports_tools:
            continue
        if tier.context_window < min_context:
            continue

        # Check if tier can handle this complexity
        tier_max_idx = complexity_levels.index(tier.max_complexity)
        if tier_max_idx >= complexity_idx:
            # Check historical success rate
            success_rate = history.tier_success_rate(tier.name, complexity)
            if success_rate >= 0.6:
                return tier
            logger.info(
                "Skipping tier %s for %s: historical success rate %.0f%%",
                tier.name,
                complexity,
                success_rate * 100,
            )
            continue

    # Fallback to the most capable tier
    return cascade[-1]


class ModelCascadeMiddleware(AgentMiddleware):
    """Middleware for cost-aware model routing.

    Automatically routes tasks to the cheapest model capable of handling them.
    Learns from outcomes to improve routing decisions over time.

    Example:
        ```python
        from bog_agents.middleware.model_cascade import (
            ModelCascadeMiddleware,
            ModelTier,
            TaskComplexity,
        )

        middleware = ModelCascadeMiddleware(
            cascade=[
                ModelTier(
                    name="fast",
                    model_id="anthropic:claude-haiku-4-5",
                    cost_per_1k_input=0.0008,
                    cost_per_1k_output=0.004,
                    max_complexity=TaskComplexity.SIMPLE,
                ),
                ModelTier(
                    name="frontier",
                    model_id="anthropic:claude-opus-4-6",
                    cost_per_1k_input=0.015,
                    cost_per_1k_output=0.075,
                    max_complexity=TaskComplexity.EXPERT,
                ),
            ],
        )
        ```
    """

    cascade: list[ModelTier]
    history: CascadeHistory
    _current_tier: ModelTier | None

    def __init__(
        self,
        *,
        cascade: list[ModelTier] | None = None,
    ) -> None:
        """Initialize model cascade middleware.

        Args:
            cascade: Custom model tiers. Uses defaults if None.
        """
        self.cascade = cascade or list(DEFAULT_CASCADE)
        self.history = CascadeHistory()
        self._current_tier = None

    def route(
        self,
        message: str,
        *,
        tool_count: int = 0,
        turn_count: int = 0,
        require_vision: bool = False,
    ) -> ModelTier:
        """Route a task to the appropriate model tier.

        Args:
            message: User message or task description.
            tool_count: Number of tool calls so far.
            turn_count: Conversation turns so far.
            require_vision: Whether vision is needed.

        Returns:
            Selected ModelTier.
        """
        complexity = classify_complexity(
            message,
            tool_count=tool_count,
            turn_count=turn_count,
        )
        tier = select_model_tier(
            complexity,
            self.cascade,
            self.history,
            require_vision=require_vision,
        )
        self._current_tier = tier

        decision = RoutingDecision(
            task_summary=message[:100],
            complexity=complexity,
            selected_tier=tier.name,
            model_id=tier.model_id,
        )
        self.history.record(decision)

        logger.info(
            "Model cascade: complexity=%s -> tier=%s (%s)",
            complexity,
            tier.name,
            tier.model_id,
        )
        return tier

    def record_outcome(self, success: bool) -> None:
        """Record whether the last routing decision was successful.

        Args:
            success: Whether the model produced a good result.
        """
        if self.history.decisions:
            self.history.decisions[-1].success = success

    @property
    def estimated_savings_pct(self) -> float:
        """Estimate cost savings vs. always using the frontier model."""
        if not self.history.decisions:
            return 0.0
        frontier = self.cascade[-1]
        frontier_cost = frontier.cost_per_1k_input + frontier.cost_per_1k_output
        actual_costs: list[float] = []
        for decision in self.history.decisions:
            tier = next((t for t in self.cascade if t.name == decision.selected_tier), frontier)
            actual_costs.append(tier.cost_per_1k_input + tier.cost_per_1k_output)
        if not actual_costs:
            return 0.0
        avg_actual = sum(actual_costs) / len(actual_costs)
        return max(0.0, (1.0 - avg_actual / frontier_cost) * 100)

    def _route_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Apply cost-aware routing to a pending model request.

        Extracts the latest human message, classifies its complexity via
        `route`, and — when a cheaper-but-capable tier is selected — overrides
        the request model with the resolved tier model. Any uncertainty
        (no human text, the frontier tier was chosen, or model resolution
        fails) falls through to the original request unchanged so a turn is
        never blocked by routing.

        Args:
            request: The pending model request.

        Returns:
            The (possibly model-overridden) request to forward downstream.
        """
        try:
            message = _latest_human_text(request)
            if not message:
                logger.debug("Model cascade: no human message text; passing through unchanged")
                return request

            tier = self.route(message)

            # Only downshift: the cascade is ordered cheapest-first, so the
            # most-capable (last) tier is never an override worth applying.
            if tier is self.cascade[-1] or tier.model_id == self.cascade[-1].model_id:
                logger.debug(
                    "Model cascade: selected most-capable tier %s; passing through unchanged",
                    tier.name,
                )
                return request

            from bog_agents._models import resolve_model

            resolved = resolve_model(tier.model_id)
            logger.info(
                "Model cascade: routing to cheaper tier %s (%s)",
                tier.name,
                tier.model_id,
            )
            return request.override(model=resolved)
        except Exception:
            # Routing must never crash a turn — degrade to the original request.
            logger.debug("Model cascade: routing failed; passing through unchanged", exc_info=True)
            return request

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
    ) -> ModelResponse[ResponseT]:
        """Route to the cheapest capable model tier, then call downstream."""
        return call_next(self._route_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
    ) -> ModelResponse[ResponseT]:
        """Route to the cheapest capable model tier, then call downstream."""
        return await call_next(self._route_request(request))
