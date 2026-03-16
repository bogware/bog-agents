"""What-If scenario engine middleware.

Feature #36: Portfolio stress testing and scenario analysis for financial advisors.

## Tools

- `create_scenario`: Define a market scenario (e.g., "rates +200bps")
- `apply_scenario`: Apply scenario shocks to portfolio holdings
- `compare_scenarios`: Compare outcomes across multiple scenarios
- `scenario_report`: Generate a formatted scenario analysis report

## Usage

```python
from bog_agents.middleware.scenario_engine import ScenarioEngineMiddleware

middleware = ScenarioEngineMiddleware()
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
class ScenarioShock:
    """A single market shock within a scenario."""

    factor: str
    magnitude: float
    unit: str = "%"
    description: str = ""


@dataclass
class HoldingImpact:
    """Impact of a scenario on a single holding."""

    ticker: str
    weight: float
    estimated_return: float
    pnl_contribution: float


@dataclass
class Scenario:
    """A complete market scenario with shocks and impacts."""

    scenario_id: int
    name: str
    description: str = ""
    shocks: list[ScenarioShock] = field(default_factory=list)
    impacts: list[HoldingImpact] = field(default_factory=list)
    total_portfolio_impact: float = 0.0

    def format_report(self) -> str:
        """Format scenario details."""
        lines = [
            f"### Scenario #{self.scenario_id}: {self.name}",
            f"{self.description}" if self.description else "",
            "",
            "**Shocks:**",
        ]
        for shock in self.shocks:
            lines.append(f"  - {shock.factor}: {shock.magnitude:+.1f}{shock.unit} {shock.description}")

        if self.impacts:
            lines.extend(["", "**Impact by Holding:**"])
            for imp in sorted(self.impacts, key=lambda x: x.pnl_contribution):
                lines.append(f"  {imp.ticker} ({imp.weight:.1%}): return {imp.estimated_return:+.1%}, P&L contribution {imp.pnl_contribution:+.2%}")
            lines.extend(
                [
                    "",
                    f"**Total Portfolio Impact: {self.total_portfolio_impact:+.2%}**",
                ]
            )
        return "\n".join(lines)


@dataclass
class ScenarioStore:
    """Store for scenarios."""

    scenarios: list[Scenario] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def create(self, *, name: str, description: str = "") -> Scenario:
        """Create a new scenario."""
        s = Scenario(scenario_id=self._next_id, name=name, description=description)
        self.scenarios.append(s)
        self._next_id += 1
        return s

    def get(self, scenario_id: int) -> Scenario | None:
        """Get scenario by ID."""
        for s in self.scenarios:
            if s.scenario_id == scenario_id:
                return s
        return None

    def format_comparison(self) -> str:
        """Compare all scenarios."""
        if not self.scenarios:
            return "No scenarios created yet."

        lines = ["## Scenario Comparison", ""]
        sorted_scenarios = sorted(self.scenarios, key=lambda s: s.total_portfolio_impact)
        for s in sorted_scenarios:
            shocks_str = ", ".join(f"{sh.factor} {sh.magnitude:+.1f}{sh.unit}" for sh in s.shocks)
            lines.append(f"**{s.name}** ({shocks_str}): Portfolio impact: {s.total_portfolio_impact:+.2%}")

        best = sorted_scenarios[-1]
        worst = sorted_scenarios[0]
        lines.extend(
            [
                "",
                f"Best case: {best.name} ({best.total_portfolio_impact:+.2%})",
                f"Worst case: {worst.name} ({worst.total_portfolio_impact:+.2%})",
                f"Range: {best.total_portfolio_impact - worst.total_portfolio_impact:.2%}",
            ]
        )
        return "\n".join(lines)


SCENARIO_SYSTEM_PROMPT = """## What-If Scenario Engine

You can run stress tests and scenario analysis on portfolios.

**Workflow:**
1. `create_scenario` — Define a market scenario with a name
2. `add_shock` — Add market factor shocks to the scenario
3. `add_holding_impact` — Estimate impact on each holding
4. `compare_scenarios` — Compare outcomes across all scenarios

**Common Scenarios:**
- Interest rates: +100bps, +200bps, -100bps
- Equity market: -10%, -20%, -30% (correction/bear/crash)
- Sector rotation: Tech -15%, Value +10%
- Inflation spike: CPI +2%, rates +150bps
- Recession: GDP -2%, equities -25%, bonds +5%"""


class ScenarioEngineState(TypedDict):
    """State for scenario engine middleware."""


class ScenarioEngineMiddleware(AgentMiddleware[ScenarioEngineState, ContextT, ResponseT]):
    """Middleware for What-If scenario analysis and portfolio stress testing."""

    state_schema = ScenarioEngineState

    def __init__(self) -> None:
        self.store = ScenarioStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build scenario engine tools."""
        mw = self

        def create_scenario(
            runtime: ToolRuntime[None, ScenarioEngineState],
            name: Annotated[str, "Scenario name (e.g., 'Rate Hike +200bps')"],
            description: Annotated[str, "Scenario description"] = "",
        ) -> str:
            """Create a new market scenario."""
            s = mw.store.create(name=name, description=description)
            return f"Scenario #{s.scenario_id} created: {name}"

        def add_shock(
            runtime: ToolRuntime[None, ScenarioEngineState],
            scenario_id: Annotated[int, "Scenario ID"],
            factor: Annotated[str, "Market factor (e.g., 'interest_rates', 'sp500', 'tech_sector')"],
            magnitude: Annotated[float, "Shock magnitude (e.g., 2.0 for +2%, -20.0 for -20%)"],
            unit: Annotated[str, "Unit (%, bps, pts)"] = "%",
            description: Annotated[str, "Additional context"] = "",
        ) -> str:
            """Add a market factor shock to a scenario."""
            s = mw.store.get(scenario_id)
            if not s:
                return f"Scenario {scenario_id} not found."
            s.shocks.append(ScenarioShock(factor=factor, magnitude=magnitude, unit=unit, description=description))
            return f"Shock added to {s.name}: {factor} {magnitude:+.1f}{unit}"

        def add_holding_impact(
            runtime: ToolRuntime[None, ScenarioEngineState],
            scenario_id: Annotated[int, "Scenario ID"],
            ticker: Annotated[str, "Ticker symbol"],
            weight: Annotated[float, "Portfolio weight (0.0 to 1.0)"],
            estimated_return: Annotated[float, "Estimated return under this scenario (e.g., -0.15 for -15%)"],
        ) -> str:
            """Estimate the impact of a scenario on a specific holding."""
            s = mw.store.get(scenario_id)
            if not s:
                return f"Scenario {scenario_id} not found."
            pnl = weight * estimated_return
            s.impacts.append(HoldingImpact(ticker=ticker, weight=weight, estimated_return=estimated_return, pnl_contribution=pnl))
            s.total_portfolio_impact = sum(i.pnl_contribution for i in s.impacts)
            return f"{ticker}: return {estimated_return:+.1%}, P&L contribution {pnl:+.2%}. Portfolio total: {s.total_portfolio_impact:+.2%}"

        def scenario_report(
            runtime: ToolRuntime[None, ScenarioEngineState],
            scenario_id: Annotated[int, "Scenario ID"] = 0,
        ) -> str:
            """Generate scenario report. Pass 0 for all scenarios."""
            if scenario_id > 0:
                s = mw.store.get(scenario_id)
                if not s:
                    return f"Scenario {scenario_id} not found."
                return s.format_report()
            lines = ["# Scenario Analysis Report", ""]
            for s in mw.store.scenarios:
                lines.append(s.format_report())
                lines.append("")
            return "\n".join(lines)

        def compare_scenarios(
            runtime: ToolRuntime[None, ScenarioEngineState],
        ) -> str:
            """Compare outcomes across all scenarios."""
            return mw.store.format_comparison()

        def clear_scenarios(
            runtime: ToolRuntime[None, ScenarioEngineState],
        ) -> str:
            """Clear all scenarios."""
            mw.store = ScenarioStore()
            return "All scenarios cleared."

        return [
            StructuredTool.from_function(
                name="create_scenario", description="Create a new market scenario for stress testing.", func=create_scenario
            ),
            StructuredTool.from_function(name="add_shock", description="Add a market factor shock to a scenario.", func=add_shock),
            StructuredTool.from_function(
                name="add_holding_impact", description="Estimate impact of a scenario on a holding.", func=add_holding_impact
            ),
            StructuredTool.from_function(name="scenario_report", description="Generate scenario analysis report.", func=scenario_report),
            StructuredTool.from_function(name="compare_scenarios", description="Compare outcomes across all scenarios.", func=compare_scenarios),
            StructuredTool.from_function(name="clear_scenarios", description="Clear all scenarios.", func=clear_scenarios),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject scenario engine instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, SCENARIO_SYSTEM_PROMPT))

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


__all__ = ["Scenario", "ScenarioEngineMiddleware", "ScenarioStore"]
