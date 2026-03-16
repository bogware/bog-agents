"""Peer comparison intelligence middleware.

Feature #39: Auto-identify peer groups, pull comparative financials,
generate relative valuation matrices, and highlight outliers.

## Tools

- `set_target_company`: Set the company being analyzed
- `add_peer`: Add a peer company with financial metrics
- `peer_comparison_matrix`: Generate comparative matrix
- `highlight_outliers`: Find significant deviations from peer medians

## Usage

```python
from bog_agents.middleware.peer_comparison import PeerComparisonMiddleware

middleware = PeerComparisonMiddleware()
```
"""

from __future__ import annotations

import logging
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
class CompanyMetrics:
    """Financial metrics for a company."""

    ticker: str
    name: str = ""
    market_cap: float = 0.0
    revenue: float = 0.0
    net_income: float = 0.0
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    debt_to_equity: float = 0.0
    roe: float = 0.0
    revenue_growth: float = 0.0
    profit_margin: float = 0.0
    dividend_yield: float = 0.0
    is_target: bool = False


@dataclass
class PeerGroup:
    """A peer comparison group with target and peers."""

    target: CompanyMetrics | None = None
    peers: list[CompanyMetrics] = field(default_factory=list)

    @property
    def all_companies(self) -> list[CompanyMetrics]:
        """All companies including target."""
        if self.target:
            return [self.target, *self.peers]
        return list(self.peers)

    def _metric_values(self, metric: str) -> list[float]:
        """Get non-zero values of a metric across peers."""
        return [getattr(c, metric) for c in self.peers if getattr(c, metric, 0) != 0]

    def _median(self, metric: str) -> float:
        """Median of a metric across peers."""
        values = self._metric_values(metric)
        return statistics.median(values) if values else 0.0

    def find_outliers(self, threshold: float = 1.5) -> list[tuple[str, str, float, float]]:
        """Find metrics where target deviates significantly from peer median.

        Args:
            threshold: Multiple of median to flag as outlier.

        Returns:
            List of (metric, direction, target_value, median_value) tuples.
        """
        if not self.target or not self.peers:
            return []

        metrics = ["pe_ratio", "pb_ratio", "debt_to_equity", "roe", "revenue_growth", "profit_margin", "dividend_yield"]
        outliers = []
        for metric in metrics:
            target_val = getattr(self.target, metric, 0)
            median_val = self._median(metric)
            if median_val == 0 or target_val == 0:
                continue
            ratio = target_val / median_val
            if ratio > threshold:
                outliers.append((metric, "above", target_val, median_val))
            elif ratio < (1 / threshold):
                outliers.append((metric, "below", target_val, median_val))
        return outliers

    def format_matrix(self) -> str:
        """Format peer comparison matrix."""
        companies = self.all_companies
        if not companies:
            return "No companies registered."

        metrics = [
            ("Market Cap ($B)", "market_cap", 1e9, ".1f"),
            ("Revenue ($B)", "revenue", 1e9, ".1f"),
            ("P/E Ratio", "pe_ratio", 1, ".1f"),
            ("P/B Ratio", "pb_ratio", 1, ".1f"),
            ("D/E Ratio", "debt_to_equity", 1, ".2f"),
            ("ROE", "roe", 0.01, ".1%"),
            ("Rev Growth", "revenue_growth", 0.01, ".1%"),
            ("Profit Margin", "profit_margin", 0.01, ".1%"),
            ("Div Yield", "dividend_yield", 0.01, ".1%"),
        ]

        lines = ["## Peer Comparison Matrix", ""]

        # Header
        header = "| Metric | " + " | ".join(f"**{c.ticker}**{'*' if c.is_target else ''}" for c in companies) + " | Peer Median |"
        lines.append(header)
        lines.append("| " + " | ".join("---" for _ in range(len(companies) + 2)) + " |")

        # Rows
        for label, attr, divisor, fmt in metrics:
            row = f"| {label} |"
            for c in companies:
                val = getattr(c, attr, 0)
                if "%" in fmt:
                    row += f" {val:{fmt}} |"
                elif divisor != 1:
                    row += f" {val / divisor:{fmt}} |"
                else:
                    row += f" {val:{fmt}} |"

            median = self._median(attr)
            if "%" in fmt:
                row += f" {median:{fmt}} |"
            elif divisor != 1:
                row += f" {median / divisor:{fmt}} |"
            else:
                row += f" {median:{fmt}} |"
            lines.append(row)

        if self.target:
            lines.append(f"\n*{self.target.ticker} is the target company*")

        return "\n".join(lines)


PEER_SYSTEM_PROMPT = """## Peer Comparison Intelligence

You can build peer comparison matrices to analyze companies relative to their peers.

**Workflow:**
1. `set_target_company` — Set the company being analyzed
2. `add_peer` — Add peer companies with financial metrics
3. `peer_comparison_matrix` — Generate the comparison matrix
4. `highlight_outliers` — Find significant deviations from peer medians

**Key Metrics:** P/E, P/B, D/E, ROE, revenue growth, profit margin, dividend yield"""


class PeerComparisonState(TypedDict):
    """State for peer comparison middleware."""


class PeerComparisonMiddleware(AgentMiddleware[PeerComparisonState, ContextT, ResponseT]):
    """Middleware for peer comparison analysis."""

    state_schema = PeerComparisonState

    def __init__(self) -> None:
        self.group = PeerGroup()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build peer comparison tools."""
        mw = self

        def set_target_company(
            runtime: ToolRuntime[None, PeerComparisonState],
            ticker: Annotated[str, "Ticker symbol"],
            name: Annotated[str, "Company name"] = "",
            market_cap: Annotated[float, "Market cap in USD"] = 0,
            revenue: Annotated[float, "Annual revenue in USD"] = 0,
            net_income: Annotated[float, "Net income in USD"] = 0,
            pe_ratio: Annotated[float, "P/E ratio"] = 0,
            pb_ratio: Annotated[float, "P/B ratio"] = 0,
            debt_to_equity: Annotated[float, "Debt-to-equity ratio"] = 0,
            roe: Annotated[float, "Return on equity (decimal)"] = 0,
            revenue_growth: Annotated[float, "Revenue growth rate (decimal)"] = 0,
            profit_margin: Annotated[float, "Net profit margin (decimal)"] = 0,
            dividend_yield: Annotated[float, "Dividend yield (decimal)"] = 0,
        ) -> str:
            """Set the target company for peer analysis."""
            mw.group.target = CompanyMetrics(
                ticker=ticker,
                name=name or ticker,
                market_cap=market_cap,
                revenue=revenue,
                net_income=net_income,
                pe_ratio=pe_ratio,
                pb_ratio=pb_ratio,
                debt_to_equity=debt_to_equity,
                roe=roe,
                revenue_growth=revenue_growth,
                profit_margin=profit_margin,
                dividend_yield=dividend_yield,
                is_target=True,
            )
            return f"Target set: {ticker}"

        def add_peer(
            runtime: ToolRuntime[None, PeerComparisonState],
            ticker: Annotated[str, "Ticker symbol"],
            name: Annotated[str, "Company name"] = "",
            market_cap: Annotated[float, "Market cap in USD"] = 0,
            revenue: Annotated[float, "Annual revenue in USD"] = 0,
            net_income: Annotated[float, "Net income in USD"] = 0,
            pe_ratio: Annotated[float, "P/E ratio"] = 0,
            pb_ratio: Annotated[float, "P/B ratio"] = 0,
            debt_to_equity: Annotated[float, "Debt-to-equity ratio"] = 0,
            roe: Annotated[float, "Return on equity (decimal)"] = 0,
            revenue_growth: Annotated[float, "Revenue growth rate (decimal)"] = 0,
            profit_margin: Annotated[float, "Net profit margin (decimal)"] = 0,
            dividend_yield: Annotated[float, "Dividend yield (decimal)"] = 0,
        ) -> str:
            """Add a peer company for comparison."""
            mw.group.peers.append(
                CompanyMetrics(
                    ticker=ticker,
                    name=name or ticker,
                    market_cap=market_cap,
                    revenue=revenue,
                    net_income=net_income,
                    pe_ratio=pe_ratio,
                    pb_ratio=pb_ratio,
                    debt_to_equity=debt_to_equity,
                    roe=roe,
                    revenue_growth=revenue_growth,
                    profit_margin=profit_margin,
                    dividend_yield=dividend_yield,
                )
            )
            return f"Peer added: {ticker}. Total peers: {len(mw.group.peers)}"

        def peer_comparison_matrix(
            runtime: ToolRuntime[None, PeerComparisonState],
        ) -> str:
            """Generate the peer comparison matrix."""
            return mw.group.format_matrix()

        def highlight_outliers(
            runtime: ToolRuntime[None, PeerComparisonState],
            threshold: Annotated[float, "Multiple of median to flag (default 1.5)"] = 1.5,
        ) -> str:
            """Find significant deviations of the target from peer medians."""
            outliers = mw.group.find_outliers(threshold)
            if not outliers:
                return "No significant outliers detected."
            lines = ["## Outlier Analysis", ""]
            for metric, direction, target_val, median_val in outliers:
                ratio = target_val / median_val if median_val else 0
                lines.append(f"- **{metric}**: {target_val:.2f} ({direction} peer median {median_val:.2f}, {ratio:.1f}x)")
            return "\n".join(lines)

        def clear_peers(
            runtime: ToolRuntime[None, PeerComparisonState],
        ) -> str:
            """Clear all peer data."""
            mw.group = PeerGroup()
            return "Peer group cleared."

        return [
            StructuredTool.from_function(
                name="set_target_company", description="Set the target company for peer comparison.", func=set_target_company
            ),
            StructuredTool.from_function(name="add_peer", description="Add a peer company with financial metrics.", func=add_peer),
            StructuredTool.from_function(name="peer_comparison_matrix", description="Generate peer comparison matrix.", func=peer_comparison_matrix),
            StructuredTool.from_function(
                name="highlight_outliers", description="Find metrics where target deviates from peers.", func=highlight_outliers
            ),
            StructuredTool.from_function(name="clear_peers", description="Clear all peer data.", func=clear_peers),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject peer comparison instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, PEER_SYSTEM_PROMPT))

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


__all__ = ["CompanyMetrics", "PeerComparisonMiddleware", "PeerGroup"]
