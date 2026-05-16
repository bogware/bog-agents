"""Portfolio analysis tools middleware.
Feature #12: Risk metrics, Monte Carlo simulation, factor attribution,
asset correlation, and benchmark comparison for financial advisor workflows.

## Tools

- `portfolio_metrics`: Calculate risk metrics (Sharpe, Sortino, max drawdown, VaR)
- `correlation_matrix`: Generate asset correlation matrix
- `monte_carlo`: Run Monte Carlo simulation for portfolio projections
- `benchmark_compare`: Compare portfolio performance against benchmarks
- `factor_attribution`: Fama-French style factor attribution

## Usage

```python
from bog_agents.middleware.portfolio_analysis import PortfolioAnalysisMiddleware

middleware = PortfolioAnalysisMiddleware()
```

⚠ **STUB — NOT FOR PRODUCTION USE.**

This middleware is a scaffold that demonstrates the shape of a real
implementation. Its tools accept calls and return placeholder structures
so an agent can be wired against the surface, but the underlying logic
is not implemented — for example, ``fetch_quote`` returns ``price=0.0``
with a note instructing the caller to populate real data. Models that
call these tools will receive plausible-looking but **incorrect**
results.

This module ships at "Development Status :: 4 - Beta" deliberately;
see REVIEW.md P0-A for the broader plan (extract to a separate
``bog-agents-finance``-style package once the implementations are real,
or remove from the headline middleware list if they will not be).
Do not enable in any flow whose output is consumed by a downstream
system, customer-facing surface, or compliance-relevant artifact.
"""

from __future__ import annotations

import logging
import math
import statistics
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
class Holding:
    """A single portfolio holding.

    Attributes:
        ticker: Security ticker symbol.
        name: Security name.
        weight: Portfolio weight (0.0 to 1.0).
        sector: Sector classification.
        asset_class: Asset class (equity, fixed_income, alternatives, cash).
        returns: Historical returns as a list of periodic returns.
    """

    ticker: str
    name: str = ""
    weight: float = 0.0
    sector: str = ""
    asset_class: str = "equity"
    returns: list[float] = field(default_factory=list)


@dataclass
class Portfolio:
    """A portfolio of holdings with analysis capabilities."""

    holdings: list[Holding] = field(default_factory=list)
    name: str = "Portfolio"
    benchmark_returns: list[float] = field(default_factory=list)
    risk_free_rate: float = 0.05

    @property
    def total_weight(self) -> float:
        """Sum of all holding weights."""
        return sum(h.weight for h in self.holdings)

    @property
    def portfolio_returns(self) -> list[float]:
        """Calculate weighted portfolio returns."""
        if not self.holdings:
            return []
        max_len = max((len(h.returns) for h in self.holdings if h.returns), default=0)
        if max_len == 0:
            return []
        result = []
        for i in range(max_len):
            period_return = sum(h.weight * (h.returns[i] if i < len(h.returns) else 0.0) for h in self.holdings)
            result.append(period_return)
        return result

    def mean_return(self) -> float:
        """Annualized mean return."""
        returns = self.portfolio_returns
        if not returns:
            return 0.0
        return statistics.mean(returns) * 12  # Assume monthly

    def std_dev(self) -> float:
        """Annualized standard deviation."""
        returns = self.portfolio_returns
        if len(returns) < 2:
            return 0.0
        return statistics.stdev(returns) * math.sqrt(12)

    def sharpe_ratio(self) -> float:
        """Sharpe ratio using risk-free rate."""
        sd = self.std_dev()
        if sd == 0:
            return 0.0
        return (self.mean_return() - self.risk_free_rate) / sd

    def sortino_ratio(self) -> float:
        """Sortino ratio (downside deviation only)."""
        returns = self.portfolio_returns
        if not returns:
            return 0.0
        downside = [r for r in returns if r < 0]
        if len(downside) < 2:
            return 0.0
        downside_dev = statistics.stdev(downside) * math.sqrt(12)
        if downside_dev == 0:
            return 0.0
        return (self.mean_return() - self.risk_free_rate) / downside_dev

    def max_drawdown(self) -> float:
        """Maximum drawdown from peak."""
        returns = self.portfolio_returns
        if not returns:
            return 0.0
        cumulative = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in returns:
            cumulative *= 1 + r
            peak = max(peak, cumulative)
            dd = (peak - cumulative) / peak
            max_dd = max(max_dd, dd)
        return max_dd

    def var_95(self) -> float:
        """Value at Risk at 95% confidence (parametric)."""
        returns = self.portfolio_returns
        if len(returns) < 2:
            return 0.0
        mean = statistics.mean(returns)
        sd = statistics.stdev(returns)
        return -(mean - 1.645 * sd)

    def allocation_by_sector(self) -> dict[str, float]:
        """Portfolio allocation grouped by sector."""
        sectors: dict[str, float] = {}
        for h in self.holdings:
            key = h.sector or "Unknown"
            sectors[key] = sectors.get(key, 0.0) + h.weight
        return dict(sorted(sectors.items(), key=lambda x: -x[1]))

    def allocation_by_asset_class(self) -> dict[str, float]:
        """Portfolio allocation grouped by asset class."""
        classes: dict[str, float] = {}
        for h in self.holdings:
            key = h.asset_class or "Unknown"
            classes[key] = classes.get(key, 0.0) + h.weight
        return dict(sorted(classes.items(), key=lambda x: -x[1]))

    def format_metrics(self) -> str:
        """Format a complete risk metrics report."""
        lines = [
            f"## Portfolio Risk Metrics: {self.name}",
            f"Holdings: {len(self.holdings)} | Total Weight: {self.total_weight:.1%}",
            "",
            "### Return & Risk",
            f"  Annualized Return:    {self.mean_return():>8.2%}",
            f"  Annualized Std Dev:   {self.std_dev():>8.2%}",
            f"  Sharpe Ratio:         {self.sharpe_ratio():>8.2f}",
            f"  Sortino Ratio:        {self.sortino_ratio():>8.2f}",
            f"  Max Drawdown:         {self.max_drawdown():>8.2%}",
            f"  VaR (95%):            {self.var_95():>8.2%}",
            f"  Risk-Free Rate:       {self.risk_free_rate:>8.2%}",
            "",
        ]

        sectors = self.allocation_by_sector()
        if sectors:
            lines.append("### Sector Allocation")
            for sector, weight in sectors.items():
                lines.append(f"  {sector:<20s} {weight:>8.1%}")
            lines.append("")

        classes = self.allocation_by_asset_class()
        if classes:
            lines.append("### Asset Class Allocation")
            for ac, weight in classes.items():
                lines.append(f"  {ac:<20s} {weight:>8.1%}")

        return "\n".join(lines)


PORTFOLIO_SYSTEM_PROMPT = """## Portfolio Analysis Tools

You have access to portfolio analysis tools for financial advisor workflows.

**Available Analysis:**
- Risk metrics: Sharpe ratio, Sortino ratio, max drawdown, VaR
- Allocation analysis by sector and asset class
- Monte Carlo simulation for future projections
- Benchmark comparison

**Workflow:**
1. Use `add_holding` to build the portfolio
2. Use `set_benchmark_returns` if benchmark comparison is needed
3. Use `portfolio_metrics` to calculate risk metrics
4. Use `monte_carlo_sim` for forward projections

Always present metrics with appropriate context — a Sharpe ratio alone means
nothing without the time period and benchmark comparison."""


class PortfolioAnalysisState(TypedDict):
    """State for portfolio analysis middleware."""


class PortfolioAnalysisMiddleware(AgentMiddleware[PortfolioAnalysisState, ContextT, ResponseT]):
    """Middleware for portfolio analysis and risk metrics.

    Provides tools for building portfolios, calculating risk metrics,
    running Monte Carlo simulations, and comparing benchmarks.
    """

    state_schema = PortfolioAnalysisState

    def __init__(self, *, risk_free_rate: float = 0.05) -> None:
        self.portfolio = Portfolio(risk_free_rate=risk_free_rate)
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build portfolio analysis tools."""
        mw = self

        def add_holding(
            runtime: ToolRuntime[None, PortfolioAnalysisState],
            ticker: Annotated[str, "Ticker symbol"],
            weight: Annotated[float, "Portfolio weight (0.0 to 1.0)"],
            name: Annotated[str, "Security name"] = "",
            sector: Annotated[str, "Sector classification"] = "",
            asset_class: Annotated[str, "Asset class: equity, fixed_income, alternatives, cash"] = "equity",
            returns: Annotated[str, "Comma-separated historical monthly returns (e.g., '0.02,-0.01,0.03')"] = "",
        ) -> str:
            """Add a holding to the portfolio with optional historical returns."""
            ret_list = [float(r.strip()) for r in returns.split(",") if r.strip()] if returns else []
            holding = Holding(
                ticker=ticker,
                name=name or ticker,
                weight=weight,
                sector=sector,
                asset_class=asset_class,
                returns=ret_list,
            )
            mw.portfolio.holdings.append(holding)
            return f"Added {ticker} ({weight:.1%} weight, {len(ret_list)} return periods). Total holdings: {len(mw.portfolio.holdings)}"

        def portfolio_metrics(
            runtime: ToolRuntime[None, PortfolioAnalysisState],
        ) -> str:
            """Calculate and display comprehensive portfolio risk metrics."""
            if not mw.portfolio.holdings:
                return "No holdings in portfolio. Use `add_holding` first."
            return mw.portfolio.format_metrics()

        def monte_carlo_sim(
            runtime: ToolRuntime[None, PortfolioAnalysisState],
            periods: Annotated[int, "Number of future periods to simulate"] = 60,
            simulations: Annotated[int, "Number of simulation runs"] = 1000,
            initial_value: Annotated[float, "Starting portfolio value"] = 100000,
        ) -> str:
            """Run Monte Carlo simulation for portfolio projections."""
            import random

            returns = mw.portfolio.portfolio_returns
            if len(returns) < 2:
                return "Insufficient return data. Add holdings with historical returns first."

            mean = statistics.mean(returns)
            sd = statistics.stdev(returns)

            final_values = []
            for _ in range(min(simulations, 10000)):
                value = initial_value
                for _ in range(periods):
                    ret = random.gauss(mean, sd)
                    value *= 1 + ret
                final_values.append(value)

            final_values.sort()
            n = len(final_values)
            p5 = final_values[int(n * 0.05)]
            p25 = final_values[int(n * 0.25)]
            p50 = final_values[int(n * 0.50)]
            p75 = final_values[int(n * 0.75)]
            p95 = final_values[int(n * 0.95)]

            lines = [
                f"## Monte Carlo Simulation ({simulations} runs, {periods} periods)",
                f"Initial Value: ${initial_value:,.0f}",
                "",
                "### Projected Final Values",
                f"   5th Percentile:  ${p5:>12,.0f}",
                f"  25th Percentile:  ${p25:>12,.0f}",
                f"  50th Percentile:  ${p50:>12,.0f} (median)",
                f"  75th Percentile:  ${p75:>12,.0f}",
                f"  95th Percentile:  ${p95:>12,.0f}",
                "",
                f"  Mean:             ${statistics.mean(final_values):>12,.0f}",
                f"  Probability of loss: {sum(1 for v in final_values if v < initial_value) / n:.1%}",
            ]
            return "\n".join(lines)

        def clear_portfolio(
            runtime: ToolRuntime[None, PortfolioAnalysisState],
        ) -> str:
            """Clear the portfolio to start fresh."""
            mw.portfolio = Portfolio(risk_free_rate=mw.portfolio.risk_free_rate)
            return "Portfolio cleared."

        return [
            StructuredTool.from_function(
                name="add_holding", description="Add a security holding to the portfolio with weight, sector, and optional returns.", func=add_holding
            ),
            StructuredTool.from_function(
                name="portfolio_metrics",
                description="Calculate comprehensive risk metrics (Sharpe, Sortino, VaR, drawdown, allocation).",
                func=portfolio_metrics,
            ),
            StructuredTool.from_function(
                name="monte_carlo_sim",
                description="Run Monte Carlo simulation for portfolio projections with percentile outcomes.",
                func=monte_carlo_sim,
            ),
            StructuredTool.from_function(name="clear_portfolio", description="Clear the portfolio to start a new analysis.", func=clear_portfolio),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject portfolio analysis instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, PORTFOLIO_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject portfolio analysis instructions.

        Args:
            request: Model request.
            call_next: Handler.

        Returns:
            Model response.
        """
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version.

        Args:
            request: Model request.
            call_next: Async handler.

        Returns:
            Model response.
        """
        return await call_next(self.modify_request(request))


__all__ = ["Holding", "Portfolio", "PortfolioAnalysisMiddleware"]
