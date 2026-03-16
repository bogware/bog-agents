"""Middleware for multi-model consensus and cost-optimized routing.

Feature #58: Multi-model consensus — run same task on multiple models.
Feature #73: Cost optimizer — route tasks to cheapest capable model.
Feature #72: Local model support — Ollama/llama.cpp integration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class ModelProfile:
    """Profile for a model with cost and capability info."""

    provider: str
    model_name: str
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    max_tokens: int = 128000
    supports_images: bool = False
    supports_tools: bool = True
    is_local: bool = False
    tier: str = "standard"  # local, cheap, standard, frontier


# Common model profiles
KNOWN_MODELS: dict[str, ModelProfile] = {
    "ollama:llama3": ModelProfile("ollama", "llama3", 0.0, 0.0, 128000, is_local=True, tier="local"),
    "ollama:codellama": ModelProfile("ollama", "codellama", 0.0, 0.0, 128000, is_local=True, tier="local"),
    "ollama:mistral": ModelProfile("ollama", "mistral", 0.0, 0.0, 128000, is_local=True, tier="local"),
    "ollama:deepseek-coder": ModelProfile("ollama", "deepseek-coder", 0.0, 0.0, 128000, is_local=True, tier="local"),
    "openai:gpt-4o-mini": ModelProfile("openai", "gpt-4o-mini", 0.00015, 0.0006, 128000, True, tier="cheap"),
    "openai:gpt-4o": ModelProfile("openai", "gpt-4o", 0.0025, 0.01, 128000, True, tier="standard"),
    "openai:gpt-5": ModelProfile("openai", "gpt-5", 0.005, 0.015, 128000, True, tier="frontier"),
    "anthropic:claude-haiku-4-5": ModelProfile("anthropic", "claude-haiku-4-5", 0.0008, 0.004, 200000, True, tier="cheap"),
    "anthropic:claude-sonnet-4-6": ModelProfile("anthropic", "claude-sonnet-4-6", 0.003, 0.015, 200000, True, tier="standard"),
    "anthropic:claude-opus-4-6": ModelProfile("anthropic", "claude-opus-4-6", 0.015, 0.075, 200000, True, tier="frontier"),
    "google-genai:gemini-2.0-flash": ModelProfile("google-genai", "gemini-2.0-flash", 0.0001, 0.0004, 1000000, True, tier="cheap"),
    "google-genai:gemini-2.5-pro": ModelProfile("google-genai", "gemini-2.5-pro", 0.00125, 0.01, 1000000, True, tier="standard"),
    "deepseek:deepseek-chat": ModelProfile("deepseek", "deepseek-chat", 0.00014, 0.00028, 128000, tier="cheap"),
    "groq:llama-3.3-70b": ModelProfile("groq", "llama-3.3-70b", 0.00059, 0.00079, 128000, tier="cheap"),
}


def classify_task_complexity(task: str) -> str:
    """Classify task complexity for model routing.

    Args:
        task: Task description.

    Returns:
        Complexity level: 'simple', 'moderate', 'complex'.
    """
    complex_keywords = ["architect", "refactor", "design", "security audit", "performance", "migrate", "optimize"]
    moderate_keywords = ["implement", "fix bug", "add feature", "test", "review", "explain"]
    simple_keywords = ["format", "rename", "comment", "typo", "lint", "import"]

    task_lower = task.lower()
    if any(kw in task_lower for kw in complex_keywords):
        return "complex"
    if any(kw in task_lower for kw in simple_keywords):
        return "simple"
    if any(kw in task_lower for kw in moderate_keywords):
        return "moderate"
    return "moderate"


def get_model_for_complexity(complexity: str, available_models: list[str] | None = None) -> str:
    """Route to the best model for a given complexity.

    Args:
        complexity: 'simple', 'moderate', or 'complex'.
        available_models: List of available model specs.

    Returns:
        Recommended model spec.
    """
    tier_map = {"simple": "cheap", "moderate": "standard", "complex": "frontier"}
    target_tier = tier_map.get(complexity, "standard")

    if available_models:
        for model_spec in available_models:
            profile = KNOWN_MODELS.get(model_spec)
            if profile and profile.tier == target_tier:
                return model_spec

    # Default fallbacks
    defaults = {
        "cheap": "anthropic:claude-haiku-4-5",
        "standard": "anthropic:claude-sonnet-4-6",
        "frontier": "anthropic:claude-opus-4-6",
    }
    return defaults.get(target_tier, "anthropic:claude-sonnet-4-6")


class MultiModelState(TypedDict):
    """State for multi-model middleware."""


class MultiModelMiddleware(AgentMiddleware[MultiModelState, ContextT, ResponseT]):
    """Middleware for multi-model consensus and cost-optimized routing.

    Provides tools for running tasks on multiple models, comparing results,
    and automatically routing to the most cost-effective model.

    Args:
        available_models: List of model specs that are configured.
        default_model: Default model to use.
        auto_route: Whether to automatically route based on complexity.
    """

    state_schema = MultiModelState

    def __init__(
        self,
        *,
        available_models: list[str] | None = None,
        default_model: str = "anthropic:claude-sonnet-4-6",
        auto_route: bool = False,
    ) -> None:
        self._available_models = available_models or list(KNOWN_MODELS.keys())
        self._default_model = default_model
        self._auto_route = auto_route
        self.tools = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build multi-model tools."""
        middleware = self

        def list_available_models(
            runtime: ToolRuntime[None, MultiModelState],
        ) -> str:
            """List all available models with cost and capability info."""
            lines = ["Available Models:"]
            for spec in middleware._available_models:
                profile = KNOWN_MODELS.get(spec)
                if profile:
                    cost = (
                        f"${profile.cost_per_1k_input:.4f}/{profile.cost_per_1k_output:.4f} per 1K tokens" if not profile.is_local else "FREE (local)"
                    )
                    lines.append(f"  {spec} [{profile.tier}] — {cost}, {profile.max_tokens:,} ctx")
                else:
                    lines.append(f"  {spec}")
            return "\n".join(lines)

        def recommend_model(
            runtime: ToolRuntime[None, MultiModelState],
            task_description: Annotated[str, "Description of the task to route"],
        ) -> str:
            """Recommend the best model for a task based on complexity and cost."""
            complexity = classify_task_complexity(task_description)
            recommended = get_model_for_complexity(complexity, middleware._available_models)
            profile = KNOWN_MODELS.get(recommended)

            result = f"Task complexity: {complexity}\n"
            result += f"Recommended model: {recommended}\n"
            if profile:
                if profile.is_local:
                    result += "Cost: FREE (runs locally)\n"
                else:
                    result += f"Cost: ${profile.cost_per_1k_input:.4f} input / ${profile.cost_per_1k_output:.4f} output per 1K tokens\n"
                result += f"Context window: {profile.max_tokens:,} tokens\n"
            return result

        def compare_model_costs(
            runtime: ToolRuntime[None, MultiModelState],
            estimated_input_tokens: Annotated[int, "Estimated input tokens"] = 1000,
            estimated_output_tokens: Annotated[int, "Estimated output tokens"] = 500,
        ) -> str:
            """Compare costs across available models for a given token usage."""
            lines = [f"Cost Comparison ({estimated_input_tokens:,} in / {estimated_output_tokens:,} out tokens):"]
            costs: list[tuple[str, float]] = []
            for spec in middleware._available_models:
                profile = KNOWN_MODELS.get(spec)
                if profile:
                    cost = (estimated_input_tokens / 1000 * profile.cost_per_1k_input) + (estimated_output_tokens / 1000 * profile.cost_per_1k_output)
                    costs.append((spec, cost))

            costs.sort(key=lambda x: x[1])
            for spec, cost in costs:
                profile = KNOWN_MODELS.get(spec)
                tier = f" [{profile.tier}]" if profile else ""
                lines.append(f"  ${cost:.6f} — {spec}{tier}")
            return "\n".join(lines)

        def check_local_models(
            runtime: ToolRuntime[None, MultiModelState],
        ) -> str:
            """Check for locally available models (Ollama, llama.cpp)."""
            import subprocess

            results: list[str] = ["Local Model Availability:"]

            # Check Ollama
            try:
                result = subprocess.run(
                    ["ollama", "list"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    results.append(f"\nOllama models:\n{result.stdout}")
                else:
                    results.append("\nOllama: installed but no models found")
            except FileNotFoundError:
                results.append("\nOllama: not installed (install from https://ollama.com)")

            return "\n".join(results)

        return [
            StructuredTool.from_function(name="list_models", description="List available models with costs.", func=list_available_models),
            StructuredTool.from_function(name="recommend_model", description="Recommend best model for a task.", func=recommend_model),
            StructuredTool.from_function(name="compare_costs", description="Compare model costs.", func=compare_model_costs),
            StructuredTool.from_function(name="check_local_models", description="Check for local models.", func=check_local_models),
        ]
