"""Model portfolio builder middleware.

Feature #15: Allocation optimization, rebalancing proposals, and bounds
checking for financial advisor workflows.

## Tools

- `create_model_portfolio`: Create a named model portfolio with target allocations
- `add_target_allocation`: Add a target allocation (asset_class, target_pct, min_pct, max_pct)
- `set_current_allocation`: Set current actual allocation for comparison
- `rebalancing_proposal`: Generate rebalancing trades to match targets
- `clear_model_portfolio`: Clear portfolio

## Usage

```python
from bog_agents.middleware.model_portfolio import ModelPortfolioMiddleware

middleware = ModelPortfolioMiddleware()
```
"""

from __future__ import annotations

import logging
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
class TargetAllocation:
    """A target allocation for an asset class.

    Attributes:
        asset_class: Asset class name.
        target_pct: Target allocation percentage.
        min_pct: Minimum allowed percentage.
        max_pct: Maximum allowed percentage.
        current_pct: Current actual allocation percentage.
    """

    asset_class: str
    target_pct: float = 0.0
    min_pct: float = 0.0
    max_pct: float = 100.0
    current_pct: float = 0.0


@dataclass
class RebalanceTrade:
    """A proposed rebalancing trade.

    Attributes:
        asset_class: Asset class to trade.
        direction: Trade direction (buy or sell).
        amount_pct: Amount to trade as a percentage.
        from_pct: Current allocation percentage.
        to_pct: Target allocation percentage.
    """

    asset_class: str
    direction: str
    amount_pct: float
    from_pct: float
    to_pct: float


@dataclass
class ModelPortfolio:
    """A model portfolio with target allocations and rebalancing logic.

    Attributes:
        name: Portfolio name.
        allocations: List of target allocations.
    """

    name: str = ""
    allocations: list[TargetAllocation] = field(default_factory=list)

    def add_allocation(
        self,
        asset_class: str,
        target_pct: float,
        min_pct: float = 0.0,
        max_pct: float = 100.0,
    ) -> TargetAllocation:
        """Add a target allocation.

        Args:
            asset_class: Asset class name.
            target_pct: Target allocation percentage.
            min_pct: Minimum allowed percentage.
            max_pct: Maximum allowed percentage.

        Returns:
            The created allocation.
        """
        allocation = TargetAllocation(
            asset_class=asset_class,
            target_pct=target_pct,
            min_pct=min_pct,
            max_pct=max_pct,
        )
        self.allocations.append(allocation)
        return allocation

    def set_current(self, asset_class: str, current_pct: float) -> bool:
        """Set the current actual allocation for an asset class.

        Args:
            asset_class: Asset class name.
            current_pct: Current allocation percentage.

        Returns:
            True if the asset class was found and updated.
        """
        for alloc in self.allocations:
            if alloc.asset_class == asset_class:
                alloc.current_pct = current_pct
                return True
        return False

    def generate_rebalance(self) -> list[RebalanceTrade]:
        """Generate rebalancing trades to match targets.

        Returns:
            List of proposed rebalancing trades.
        """
        trades = []
        for alloc in self.allocations:
            diff = alloc.target_pct - alloc.current_pct
            if abs(diff) < 0.01:
                continue
            direction = "buy" if diff > 0 else "sell"
            trades.append(
                RebalanceTrade(
                    asset_class=alloc.asset_class,
                    direction=direction,
                    amount_pct=abs(diff),
                    from_pct=alloc.current_pct,
                    to_pct=alloc.target_pct,
                )
            )
        return trades

    def format_proposal(self) -> str:
        """Format a rebalancing proposal as markdown.

        Returns:
            Markdown-formatted rebalancing proposal.
        """
        if not self.allocations:
            return "No allocations defined."

        lines = [
            f"## Rebalancing Proposal: {self.name}",
            f"Allocations: {len(self.allocations)} | Total Target: {sum(a.target_pct for a in self.allocations):.1f}%",
            "",
            "### Current vs Target",
            "| Asset Class | Current | Target | Min | Max | Status |",
            "| --- | --- | --- | --- | --- | --- |",
        ]

        for alloc in self.allocations:
            within = "OK" if alloc.min_pct <= alloc.current_pct <= alloc.max_pct else "OUT"
            lines.append(
                f"| {alloc.asset_class} | {alloc.current_pct:.1f}% | {alloc.target_pct:.1f}% "
                f"| {alloc.min_pct:.1f}% | {alloc.max_pct:.1f}% | {within} |"
            )

        lines.append("")

        trades = self.generate_rebalance()
        if trades:
            lines.append("### Proposed Trades")
            for trade in trades:
                arrow = "+" if trade.direction == "buy" else "-"
                lines.append(
                    f"- **{trade.direction.upper()}** {trade.asset_class}: "
                    f"{arrow}{trade.amount_pct:.1f}% ({trade.from_pct:.1f}% -> {trade.to_pct:.1f}%)"
                )
            lines.append("")
        else:
            lines.append("Portfolio is balanced. No trades needed.")
            lines.append("")

        within_bounds = self.is_within_bounds()
        lines.append(f"### Bounds Check: {'PASS' if within_bounds else 'FAIL'}")

        return "\n".join(lines)

    def is_within_bounds(self) -> bool:
        """Check if all current allocations are within min/max bounds.

        Returns:
            True if all allocations are within bounds.
        """
        return all(alloc.min_pct <= alloc.current_pct <= alloc.max_pct for alloc in self.allocations)


MODEL_PORTFOLIO_SYSTEM_PROMPT = """## Model Portfolio Builder

You have tools to build model portfolios with target allocations and generate
rebalancing proposals.

**Workflow:**
1. `create_model_portfolio` — Create a named portfolio
2. `add_target_allocation` — Define target allocations per asset class
3. `set_current_allocation` — Set actual current allocations
4. `rebalancing_proposal` — Generate trades to match targets

**Key Concepts:**
- Each allocation has target, min, and max percentages
- Rebalancing trades are generated when current != target
- Bounds check verifies current is within min/max range
- Total target allocations should sum to 100%"""


class ModelPortfolioState(TypedDict):
    """State for model portfolio middleware."""


class ModelPortfolioMiddleware(AgentMiddleware[ModelPortfolioState, ContextT, ResponseT]):
    """Middleware for model portfolio building and rebalancing."""

    state_schema = ModelPortfolioState

    def __init__(self) -> None:
        self.portfolio = ModelPortfolio()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build model portfolio tools."""
        mw = self

        def create_model_portfolio(
            runtime: ToolRuntime[None, ModelPortfolioState],
            name: Annotated[str, "Portfolio name"],
        ) -> str:
            """Create a named model portfolio with target allocations."""
            mw.portfolio = ModelPortfolio(name=name)
            return f"Model portfolio created: {name}"

        def add_target_allocation(
            runtime: ToolRuntime[None, ModelPortfolioState],
            asset_class: Annotated[str, "Asset class name (e.g., US Equity, Fixed Income)"],
            target_pct: Annotated[float, "Target allocation percentage"],
            min_pct: Annotated[float, "Minimum allowed percentage"] = 0.0,
            max_pct: Annotated[float, "Maximum allowed percentage"] = 100.0,
        ) -> str:
            """Add a target allocation to the model portfolio."""
            if not mw.portfolio.name:
                return "No portfolio created. Use `create_model_portfolio` first."
            alloc = mw.portfolio.add_allocation(
                asset_class=asset_class,
                target_pct=target_pct,
                min_pct=min_pct,
                max_pct=max_pct,
            )
            total = sum(a.target_pct for a in mw.portfolio.allocations)
            return (
                f"Added {alloc.asset_class}: {alloc.target_pct:.1f}% target "
                f"(range {alloc.min_pct:.1f}%-{alloc.max_pct:.1f}%). "
                f"Total target: {total:.1f}%"
            )

        def set_current_allocation(
            runtime: ToolRuntime[None, ModelPortfolioState],
            asset_class: Annotated[str, "Asset class name"],
            current_pct: Annotated[float, "Current actual allocation percentage"],
        ) -> str:
            """Set current actual allocation for comparison."""
            if not mw.portfolio.name:
                return "No portfolio created. Use `create_model_portfolio` first."
            found = mw.portfolio.set_current(asset_class, current_pct)
            if not found:
                return f"Asset class '{asset_class}' not found. Use `add_target_allocation` first."
            return f"Current allocation set: {asset_class} = {current_pct:.1f}%"

        def rebalancing_proposal(
            runtime: ToolRuntime[None, ModelPortfolioState],
        ) -> str:
            """Generate rebalancing trades to match targets."""
            if not mw.portfolio.name:
                return "No portfolio created. Use `create_model_portfolio` first."
            return mw.portfolio.format_proposal()

        def clear_model_portfolio(
            runtime: ToolRuntime[None, ModelPortfolioState],
        ) -> str:
            """Clear the model portfolio."""
            mw.portfolio = ModelPortfolio()
            return "Model portfolio cleared."

        return [
            StructuredTool.from_function(name="create_model_portfolio", description="Create a named model portfolio.", func=create_model_portfolio),
            StructuredTool.from_function(
                name="add_target_allocation",
                description="Add a target allocation with asset class, target, min, and max percentages.",
                func=add_target_allocation,
            ),
            StructuredTool.from_function(
                name="set_current_allocation", description="Set current actual allocation for an asset class.", func=set_current_allocation
            ),
            StructuredTool.from_function(
                name="rebalancing_proposal", description="Generate rebalancing trades to match target allocations.", func=rebalancing_proposal
            ),
            StructuredTool.from_function(name="clear_model_portfolio", description="Clear the model portfolio.", func=clear_model_portfolio),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject model portfolio instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, MODEL_PORTFOLIO_SYSTEM_PROMPT))

    def wrap_model_call(
        self, request: ModelRequest[ContextT], call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]]
    ) -> ModelResponse[ResponseT]:
        """Inject instructions."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self, request: ModelRequest[ContextT], call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]]
    ) -> ModelResponse[ResponseT]:
        """Async version."""
        return await call_next(self.modify_request(request))


__all__ = ["ModelPortfolio", "ModelPortfolioMiddleware", "RebalanceTrade", "TargetAllocation"]
