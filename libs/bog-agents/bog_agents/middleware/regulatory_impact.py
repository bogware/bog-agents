"""Regulatory change impact analyzer middleware.

Feature #38: Map regulations to portfolios/holdings and assess impact
with sector overlap analysis, scoring, and comprehensive reporting.

## Tools

- `add_regulation`: Add a regulation change
- `add_holding_exposure`: Add a holding and its sector exposure
- `analyze_impact`: Analyze impact of all regulations on all holdings
- `impact_report`: Generate formatted impact assessment report
- `clear_impact_data`: Clear all data

## Usage

```python
from bog_agents.middleware.regulatory_impact import RegulatoryImpactMiddleware

middleware = RegulatoryImpactMiddleware()
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
class Regulation:
    """A regulation change.

    Attributes:
        regulation_id: Unique identifier.
        title: Regulation title.
        agency: Issuing agency.
        effective_date: Date the regulation takes effect.
        description: Detailed description.
        affected_sectors: List of affected sectors.
        severity: Severity level (high, medium, low).
    """

    regulation_id: int
    title: str
    agency: str
    effective_date: str
    description: str
    affected_sectors: list[str] = field(default_factory=list)
    severity: str = "medium"


@dataclass
class HoldingExposure:
    """A holding and its sector exposure.

    Attributes:
        ticker: Ticker symbol.
        sectors: List of sectors the holding is exposed to.
        weight: Portfolio weight (0.0 to 1.0).
    """

    ticker: str
    sectors: list[str] = field(default_factory=list)
    weight: float = 0.0


@dataclass
class ImpactAssessment:
    """An impact assessment for a holding/regulation pair.

    Attributes:
        ticker: Ticker symbol.
        regulation_title: Title of the regulation.
        overlap_sectors: Sectors that overlap between holding and regulation.
        impact_score: Computed score (weight * sector_overlap_ratio).
        risk_level: Derived risk level (high, medium, low).
    """

    ticker: str
    regulation_title: str
    overlap_sectors: list[str] = field(default_factory=list)
    impact_score: float = 0.0
    risk_level: str = "low"


@dataclass
class ImpactStore:
    """Store for regulations, holdings, and impact assessments.

    Attributes:
        regulations: List of all regulations.
        holdings: List of all holdings.
        assessments: List of computed impact assessments.
    """

    regulations: list[Regulation] = field(default_factory=list)
    holdings: list[HoldingExposure] = field(default_factory=list)
    assessments: list[ImpactAssessment] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def add_regulation(
        self,
        title: str,
        agency: str,
        effective_date: str,
        description: str,
        affected_sectors: list[str],
        severity: str = "medium",
    ) -> Regulation:
        """Add a regulation change.

        Args:
            title: Regulation title.
            agency: Issuing agency.
            effective_date: Effective date.
            description: Description.
            affected_sectors: Affected sectors.
            severity: Severity level.

        Returns:
            The created regulation.
        """
        regulation = Regulation(
            regulation_id=self._next_id,
            title=title,
            agency=agency,
            effective_date=effective_date,
            description=description,
            affected_sectors=affected_sectors,
            severity=severity,
        )
        self.regulations.append(regulation)
        self._next_id += 1
        return regulation

    def add_holding(
        self,
        ticker: str,
        sectors: list[str],
        weight: float,
    ) -> HoldingExposure:
        """Add a holding and its sector exposure.

        Args:
            ticker: Ticker symbol.
            sectors: Sectors the holding is exposed to.
            weight: Portfolio weight.

        Returns:
            The created holding exposure.
        """
        holding = HoldingExposure(ticker=ticker, sectors=sectors, weight=weight)
        self.holdings.append(holding)
        return holding

    def analyze(self) -> list[ImpactAssessment]:
        """Analyze impact of all regulations on all holdings.

        Computes the cross-product of regulations and holdings, scoring
        each pair by sector overlap ratio multiplied by portfolio weight.

        Returns:
            List of impact assessments.
        """
        self.assessments = []
        for regulation in self.regulations:
            reg_sectors = set(regulation.affected_sectors)
            for holding in self.holdings:
                hold_sectors = set(holding.sectors)
                overlap = reg_sectors & hold_sectors
                if not overlap:
                    continue
                overlap_ratio = len(overlap) / len(reg_sectors) if reg_sectors else 0.0
                score = holding.weight * overlap_ratio
                if score >= 0.05:
                    risk_level = "high"
                elif score >= 0.02:
                    risk_level = "medium"
                else:
                    risk_level = "low"
                assessment = ImpactAssessment(
                    ticker=holding.ticker,
                    regulation_title=regulation.title,
                    overlap_sectors=sorted(overlap),
                    impact_score=round(score, 4),
                    risk_level=risk_level,
                )
                self.assessments.append(assessment)
        self.assessments.sort(key=lambda a: a.impact_score, reverse=True)
        return self.assessments

    def format_report(self) -> str:
        """Format comprehensive impact assessment report.

        Returns:
            Markdown-formatted impact report with overall exposure summary,
            per-regulation breakdown, and most-affected holdings.
        """
        lines = [
            "## Regulatory Impact Assessment Report",
            f"**Regulations:** {len(self.regulations)}",
            f"**Holdings:** {len(self.holdings)}",
            f"**Impact Assessments:** {len(self.assessments)}",
            "",
        ]

        if not self.assessments:
            lines.append("No impact assessments computed. Run `analyze_impact` first.")
            return "\n".join(lines)

        # Overall Exposure Summary
        lines.append("### Overall Exposure Summary")
        lines.append("")
        high = sum(1 for a in self.assessments if a.risk_level == "high")
        medium = sum(1 for a in self.assessments if a.risk_level == "medium")
        low = sum(1 for a in self.assessments if a.risk_level == "low")
        total_score = sum(a.impact_score for a in self.assessments)
        lines.append(f"Risk breakdown: High: {high}, Medium: {medium}, Low: {low}")
        lines.append(f"Total impact score: {round(total_score, 4)}")
        lines.append("")

        # Per-Regulation Breakdown
        lines.append("### Per-Regulation Breakdown")
        lines.append("")
        reg_titles = []
        seen: set[str] = set()
        for a in self.assessments:
            if a.regulation_title not in seen:
                reg_titles.append(a.regulation_title)
                seen.add(a.regulation_title)
        for title in reg_titles:
            reg_assessments = [a for a in self.assessments if a.regulation_title == title]
            reg_score = sum(a.impact_score for a in reg_assessments)
            lines.append(f"**{title}** (total score: {round(reg_score, 4)})")
            for a in reg_assessments:
                sectors = ", ".join(a.overlap_sectors)
                lines.append(f"  - {a.ticker} [{a.risk_level.upper()}] score: {a.impact_score} | sectors: {sectors}")
            lines.append("")

        # Most-Affected Holdings
        lines.append("### Most-Affected Holdings")
        lines.append("")
        ticker_scores: dict[str, float] = {}
        ticker_assessments: dict[str, list[ImpactAssessment]] = {}
        for a in self.assessments:
            ticker_scores[a.ticker] = ticker_scores.get(a.ticker, 0.0) + a.impact_score
            ticker_assessments.setdefault(a.ticker, []).append(a)
        sorted_tickers = sorted(ticker_scores.items(), key=lambda x: x[1], reverse=True)
        for ticker, total in sorted_tickers:
            assessments = ticker_assessments[ticker]
            high_count = sum(1 for a in assessments if a.risk_level == "high")
            lines.append(f"- **{ticker}** total score: {round(total, 4)} ({len(assessments)} regulation(s), {high_count} high-risk)")
        lines.append("")

        return "\n".join(lines)


REGULATORY_IMPACT_SYSTEM_PROMPT = """## Regulatory Change Impact Analyzer

You have tools to map regulations to portfolio holdings and assess impact.

**Severity Levels:** high, medium, low
**Risk Levels:** high (score >= 0.05), medium (score >= 0.02), low (score < 0.02)

**Workflow:**
1. `add_regulation` — Add regulation changes with affected sectors
2. `add_holding_exposure` — Add portfolio holdings with sector exposure and weight
3. `analyze_impact` — Compute cross-product impact of regulations on holdings
4. `impact_report` — Generate formatted impact assessment report
5. `clear_impact_data` — Reset all data

Impact scores are computed as portfolio weight multiplied by sector overlap ratio."""


class RegulatoryImpactState(TypedDict):
    """State for regulatory impact middleware."""


class RegulatoryImpactMiddleware(AgentMiddleware[RegulatoryImpactState, ContextT, ResponseT]):
    """Middleware for analyzing regulatory change impact on portfolios."""

    state_schema = RegulatoryImpactState

    def __init__(self) -> None:
        self.store = ImpactStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build regulatory impact tools."""
        mw = self

        def add_regulation(
            runtime: ToolRuntime[None, RegulatoryImpactState],
            title: Annotated[str, "Regulation title"],
            agency: Annotated[str, "Issuing agency (e.g., SEC, EPA, CFPB)"],
            effective_date: Annotated[str, "Effective date (YYYY-MM-DD)"],
            description: Annotated[str, "Description of the regulation change"],
            affected_sectors: Annotated[str, "Comma-separated list of affected sectors"],
            severity: Annotated[str, "Severity: high, medium, low"] = "medium",
        ) -> str:
            """Add a regulation change."""
            sectors = [s.strip() for s in affected_sectors.split(",") if s.strip()]
            regulation = mw.store.add_regulation(
                title=title,
                agency=agency,
                effective_date=effective_date,
                description=description,
                affected_sectors=sectors,
                severity=severity,
            )
            return (
                f"Regulation #{regulation.regulation_id} added: {title} "
                f"({agency}, {severity}). Affected sectors: {', '.join(sectors)}. "
                f"Total regulations: {len(mw.store.regulations)}"
            )

        def add_holding_exposure(
            runtime: ToolRuntime[None, RegulatoryImpactState],
            ticker: Annotated[str, "Ticker symbol"],
            sectors: Annotated[str, "Comma-separated list of sectors the holding is exposed to"],
            weight: Annotated[float, "Portfolio weight (0.0 to 1.0)"],
        ) -> str:
            """Add a holding and its sector exposure."""
            sector_list = [s.strip() for s in sectors.split(",") if s.strip()]
            holding = mw.store.add_holding(
                ticker=ticker,
                sectors=sector_list,
                weight=weight,
            )
            return (
                f"Holding added: {holding.ticker} (weight: {holding.weight}, "
                f"sectors: {', '.join(sector_list)}). "
                f"Total holdings: {len(mw.store.holdings)}"
            )

        def analyze_impact(
            runtime: ToolRuntime[None, RegulatoryImpactState],
        ) -> str:
            """Analyze impact of all regulations on all holdings."""
            if not mw.store.regulations:
                return "Error: No regulations added. Use `add_regulation` first."
            if not mw.store.holdings:
                return "Error: No holdings added. Use `add_holding_exposure` first."
            assessments = mw.store.analyze()
            if not assessments:
                return "Analysis complete: No sector overlaps found between regulations and holdings."
            high = sum(1 for a in assessments if a.risk_level == "high")
            medium = sum(1 for a in assessments if a.risk_level == "medium")
            low = sum(1 for a in assessments if a.risk_level == "low")
            return (
                f"Impact analysis complete: {len(assessments)} assessment(s) generated. "
                f"Risk breakdown — High: {high}, Medium: {medium}, Low: {low}. "
                f"Use `impact_report` for full details."
            )

        def impact_report(
            runtime: ToolRuntime[None, RegulatoryImpactState],
        ) -> str:
            """Generate formatted impact assessment report."""
            return mw.store.format_report()

        def clear_impact_data(
            runtime: ToolRuntime[None, RegulatoryImpactState],
        ) -> str:
            """Clear all regulations, holdings, and assessments."""
            mw.store = ImpactStore()
            return "All regulatory impact data cleared."

        return [
            StructuredTool.from_function(name="add_regulation", description="Add a regulation change with affected sectors.", func=add_regulation),
            StructuredTool.from_function(
                name="add_holding_exposure", description="Add a holding and its sector exposure.", func=add_holding_exposure
            ),
            StructuredTool.from_function(
                name="analyze_impact", description="Analyze impact of all regulations on all holdings.", func=analyze_impact
            ),
            StructuredTool.from_function(name="impact_report", description="Generate formatted impact assessment report.", func=impact_report),
            StructuredTool.from_function(
                name="clear_impact_data", description="Clear all regulations, holdings, and assessments.", func=clear_impact_data
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject regulatory impact instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, REGULATORY_IMPACT_SYSTEM_PROMPT))

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


__all__ = ["HoldingExposure", "ImpactAssessment", "ImpactStore", "Regulation", "RegulatoryImpactMiddleware"]
