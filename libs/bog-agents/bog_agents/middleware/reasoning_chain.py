"""Explainable reasoning chain middleware.

Feature #34: Full chain-of-reasoning provenance for every agent conclusion.

## Overview

Records the reasoning chain for every agent action: what data was consulted,
what conclusions were drawn, and how they connect. Produces a visual reasoning
graph and structured explanation suitable for FINRA supervisory documentation.

## Usage

```python
from bog_agents.middleware.reasoning_chain import ReasoningChainMiddleware

middleware = ReasoningChainMiddleware()
```
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

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

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


@dataclass
class ReasoningStep:
    """A single step in a reasoning chain.

    Attributes:
        step_id: Unique identifier for this step.
        step_type: Type of step (observation, inference, conclusion, assumption, lookup).
        content: The reasoning content.
        data_sources: Sources consulted for this step.
        depends_on: IDs of steps this step depends on.
        confidence: Confidence level (0.0 to 1.0).
        timestamp: When this step was recorded.
    """

    step_id: int
    step_type: str
    content: str
    data_sources: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    confidence: float = 1.0
    timestamp: str = ""


@dataclass
class ReasoningChain:
    """A complete reasoning chain for a research task."""

    steps: list[ReasoningStep] = field(default_factory=list)
    conclusion: str = ""
    _next_id: int = field(default=1, repr=False)

    def add_step(
        self,
        *,
        step_type: str,
        content: str,
        data_sources: list[str] | None = None,
        depends_on: list[int] | None = None,
        confidence: float = 1.0,
    ) -> ReasoningStep:
        """Add a step to the reasoning chain.

        Args:
            step_type: Type of step.
            content: The reasoning content.
            data_sources: Sources consulted.
            depends_on: IDs of prerequisite steps.
            confidence: Confidence level (0.0 to 1.0).

        Returns:
            The newly created step.
        """
        step = ReasoningStep(
            step_id=self._next_id,
            step_type=step_type,
            content=content,
            data_sources=data_sources or [],
            depends_on=depends_on or [],
            confidence=max(0.0, min(1.0, confidence)),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.steps.append(step)
        self._next_id += 1
        return step

    def set_conclusion(self, conclusion: str) -> None:
        """Set the final conclusion for this reasoning chain.

        Args:
            conclusion: The final conclusion text.
        """
        self.conclusion = conclusion

    @property
    def overall_confidence(self) -> float:
        """Calculate overall chain confidence as the minimum step confidence."""
        if not self.steps:
            return 0.0
        return min(s.confidence for s in self.steps)

    def format_chain(self) -> str:
        """Format the reasoning chain as a readable document.

        Returns:
            Formatted reasoning chain string.
        """
        if not self.steps:
            return "No reasoning steps recorded yet."

        lines = [
            "## Reasoning Chain",
            f"Steps: {len(self.steps)} | Overall confidence: {self.overall_confidence:.0%}",
            "",
        ]

        for step in self.steps:
            conf = f"{step.confidence:.0%}"
            deps = f" (depends on: {', '.join(f'#{d}' for d in step.depends_on)})" if step.depends_on else ""
            lines.append(f"**Step #{step.step_id}** [{step.step_type}] — Confidence: {conf}{deps}")
            lines.append(f"  {step.content}")
            if step.data_sources:
                lines.append(f"  Sources: {', '.join(step.data_sources)}")
            lines.append("")

        if self.conclusion:
            lines.append(f"### Conclusion (confidence: {self.overall_confidence:.0%})")
            lines.append(self.conclusion)

        return "\n".join(lines)

    def format_graph(self) -> str:
        """Format a text-based dependency graph of the reasoning chain.

        Returns:
            ASCII representation of the reasoning graph.
        """
        if not self.steps:
            return "No reasoning steps to graph."

        lines = ["## Reasoning Graph", ""]
        for step in self.steps:
            prefix = {"observation": "O", "inference": "I", "conclusion": "C", "assumption": "A", "lookup": "L"}.get(step.step_type, "?")
            node = f"[{prefix}{step.step_id}] {step.content[:60]}"
            if step.depends_on:
                arrows = " <- " + ", ".join(f"[{prefix}{d}]" for d in step.depends_on)
                node += arrows
            lines.append(node)

        if self.conclusion:
            lines.append(f"\n=> CONCLUSION: {self.conclusion[:80]}")

        return "\n".join(lines)

    def clear(self) -> None:
        """Clear the reasoning chain for a new analysis."""
        self.steps.clear()
        self.conclusion = ""
        self._next_id = 1


REASONING_SYSTEM_PROMPT = """## Explainable Reasoning

You MUST document your reasoning process using the reasoning chain tools.

**Step Types:**
- `observation` — A fact or data point you've observed from a source
- `inference` — A logical conclusion drawn from one or more observations
- `assumption` — Something you're assuming without direct evidence (flag these!)
- `lookup` — Data retrieved from a specific source
- `conclusion` — A final recommendation or finding

**Workflow:**
1. Record each `observation` or `lookup` as you gather information
2. Record each `inference` with `depends_on` linking to the observations it's based on
3. Flag any `assumption` clearly with reduced confidence
4. Record the final `conclusion` with all dependencies

**Confidence Levels:**
- 1.0: Directly verified from primary source
- 0.8: Strong secondary source or consistent across multiple sources
- 0.5: Single secondary source, not independently verified
- 0.3: Inferred or estimated, limited supporting evidence
- 0.1: Assumption or speculation

Use `show_reasoning` to review the current chain, and `reasoning_graph` for the dependency view."""


class ReasoningChainState(TypedDict):
    """State for reasoning chain middleware."""


class ReasoningChainMiddleware(AgentMiddleware[ReasoningChainState, ContextT, ResponseT]):
    """Middleware for explainable reasoning chain tracking.

    Records step-by-step reasoning with dependencies, confidence levels,
    and data source provenance. Provides tools for viewing and managing
    the reasoning chain.
    """

    state_schema = ReasoningChainState

    def __init__(self) -> None:
        self.chain = ReasoningChain()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build reasoning chain tools."""
        mw = self

        def add_reasoning_step(
            runtime: ToolRuntime[None, ReasoningChainState],
            step_type: Annotated[str, "Step type: observation, inference, assumption, lookup, or conclusion"],
            content: Annotated[str, "The reasoning content"],
            data_sources: Annotated[str, "Comma-separated data sources consulted"] = "",
            depends_on: Annotated[str, "Comma-separated step IDs this depends on"] = "",
            confidence: Annotated[float, "Confidence level from 0.0 to 1.0"] = 1.0,
        ) -> str:
            """Record a reasoning step in the chain."""
            sources = [s.strip() for s in data_sources.split(",") if s.strip()] if data_sources else []
            deps = [int(d.strip()) for d in depends_on.split(",") if d.strip()] if depends_on else []
            step = mw.chain.add_step(
                step_type=step_type,
                content=content,
                data_sources=sources,
                depends_on=deps,
                confidence=confidence,
            )
            return f"Step #{step.step_id} [{step_type}] recorded (confidence: {confidence:.0%})"

        def set_conclusion(
            runtime: ToolRuntime[None, ReasoningChainState],
            conclusion: Annotated[str, "The final conclusion or recommendation"],
        ) -> str:
            """Set the final conclusion for the current reasoning chain."""
            mw.chain.set_conclusion(conclusion)
            return f"Conclusion set (overall confidence: {mw.chain.overall_confidence:.0%}): {conclusion[:100]}"

        def show_reasoning(
            runtime: ToolRuntime[None, ReasoningChainState],
        ) -> str:
            """Show the complete reasoning chain with all steps and dependencies."""
            return mw.chain.format_chain()

        def reasoning_graph(
            runtime: ToolRuntime[None, ReasoningChainState],
        ) -> str:
            """Show a text-based dependency graph of the reasoning chain."""
            return mw.chain.format_graph()

        def clear_reasoning(
            runtime: ToolRuntime[None, ReasoningChainState],
        ) -> str:
            """Clear the reasoning chain to start a new analysis."""
            mw.chain.clear()
            return "Reasoning chain cleared. Ready for new analysis."

        return [
            StructuredTool.from_function(
                name="add_reasoning_step",
                description="Record a step in the reasoning chain with type, content, sources, dependencies, and confidence.",
                func=add_reasoning_step,
            ),
            StructuredTool.from_function(
                name="set_conclusion",
                description="Set the final conclusion for the current reasoning chain.",
                func=set_conclusion,
            ),
            StructuredTool.from_function(
                name="show_reasoning",
                description="Show the complete reasoning chain with all steps, dependencies, and confidence levels.",
                func=show_reasoning,
            ),
            StructuredTool.from_function(
                name="reasoning_graph",
                description="Show a text-based dependency graph of the reasoning chain.",
                func=reasoning_graph,
            ),
            StructuredTool.from_function(
                name="clear_reasoning",
                description="Clear the reasoning chain to start a new analysis.",
                func=clear_reasoning,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject reasoning chain requirements into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with reasoning instructions.
        """
        new_system_message = append_to_system_message(request.system_message, REASONING_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject reasoning chain requirements.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = ["ReasoningChain", "ReasoningChainMiddleware", "ReasoningStep"]
