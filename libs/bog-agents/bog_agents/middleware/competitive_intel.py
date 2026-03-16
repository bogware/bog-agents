"""Competitive Intel Tracker middleware for monitoring competitor filings and activity."""

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
class CompetitorProfile:
    """Profile of a tracked competitor."""

    name: str
    ticker: str
    sector: str
    market_cap: str
    description: str


@dataclass
class IntelEvent:
    """A competitive intelligence event."""

    event_id: int
    competitor: str
    event_type: str  # filing, earnings, product_launch, leadership_change, acquisition, partnership, regulatory, other
    title: str
    description: str
    source: str
    date: str
    impact: str  # high, medium, low
    timestamp: str


@dataclass
class IntelStore:
    """Storage for competitive intelligence data."""

    competitors: dict[str, CompetitorProfile] = field(default_factory=dict)
    events: list[IntelEvent] = field(default_factory=list)
    _next_event_id: int = 1

    def add_competitor(
        self,
        name: str,
        ticker: str,
        sector: str,
        market_cap: str,
        description: str,
    ) -> CompetitorProfile:
        """Add a competitor profile."""
        profile = CompetitorProfile(
            name=name,
            ticker=ticker.upper(),
            sector=sector,
            market_cap=market_cap,
            description=description,
        )
        self.competitors[ticker.upper()] = profile
        return profile

    def add_event(
        self,
        competitor: str,
        event_type: str,
        title: str,
        description: str,
        source: str,
        date: str,
        impact: str,
    ) -> IntelEvent:
        """Add a competitive intelligence event."""
        event = IntelEvent(
            event_id=self._next_event_id,
            competitor=competitor.upper(),
            event_type=event_type,
            title=title,
            description=description,
            source=source,
            date=date,
            impact=impact,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self._next_event_id += 1
        self.events.append(event)
        return event

    def get_competitor_events(self, ticker: str) -> list[IntelEvent]:
        """Get all events for a specific competitor."""
        return [e for e in self.events if e.competitor == ticker.upper()]

    def format_briefing(self, ticker: str | None = None) -> str:
        """Format a competitive intelligence briefing."""
        lines = ["# Competitive Intelligence Briefing", ""]

        targets = {ticker.upper(): self.competitors[ticker.upper()]} if ticker and ticker.upper() in self.competitors else self.competitors

        if not targets:
            return "No competitors tracked."

        for tk, profile in sorted(targets.items()):
            lines.append(f"## {profile.name} ({tk})")
            lines.append(f"Sector: {profile.sector} | Market Cap: {profile.market_cap}")
            lines.append(f"{profile.description}")
            lines.append("")

            events = self.get_competitor_events(tk)
            if events:
                lines.append("### Recent Events")
                for e in events:
                    impact_marker = {"high": "!!!", "medium": "!!", "low": "!"}.get(e.impact, "!")
                    lines.append(f"  [{impact_marker}] {e.date} — {e.title} ({e.event_type})")
                    lines.append(f"      {e.description}")
                    lines.append(f"      Source: {e.source}")
            else:
                lines.append("No events recorded.")
            lines.append("")
        return "\n".join(lines)


SYSTEM_PROMPT = """You have access to competitive intelligence tools for monitoring competitor filings \
and activity. Event types: filing, earnings, product_launch, leadership_change, acquisition, partnership, \
regulatory, other. Impact levels: high, medium, low. Use these tools to track competitors, log events, \
and generate intelligence briefings."""


class CompetitiveIntelState(TypedDict):
    """State for competitive intelligence middleware."""


class CompetitiveIntelMiddleware(AgentMiddleware[CompetitiveIntelState, ContextT, ResponseT]):
    """Middleware for tracking competitor filings, events, and activity."""

    state_schema = CompetitiveIntelState

    def __init__(self) -> None:
        self.store = IntelStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build the competitive intelligence tools."""
        mw = self

        def add_competitor(
            runtime: ToolRuntime[None, CompetitiveIntelState],
            name: Annotated[str, "Company name"],
            ticker: Annotated[str, "Stock ticker symbol"],
            sector: Annotated[str, "Industry sector"],
            market_cap: Annotated[str, "Market capitalization (e.g., '50B')"],
            description: Annotated[str, "Brief description of the competitor"],
        ) -> str:
            """Add a competitor profile to track."""
            profile = mw.store.add_competitor(name, ticker, sector, market_cap, description)
            logger.info("Added competitor: %s (%s)", name, profile.ticker)
            return f"Competitor added: {profile.name} ({profile.ticker}) — {profile.sector}, market cap: {profile.market_cap}."

        def add_intel_event(
            runtime: ToolRuntime[None, CompetitiveIntelState],
            competitor: Annotated[str, "Ticker of the competitor"],
            event_type: Annotated[
                str,
                "Event type: filing, earnings, product_launch, leadership_change, acquisition, partnership, regulatory, or other",
            ],
            title: Annotated[str, "Event title"],
            description: Annotated[str, "Event description"],
            source: Annotated[str, "Information source"],
            date: Annotated[str, "Event date (YYYY-MM-DD)"],
            impact: Annotated[str, "Impact level: high, medium, or low"],
        ) -> str:
            """Add a competitive intelligence event."""
            valid_types = (
                "filing",
                "earnings",
                "product_launch",
                "leadership_change",
                "acquisition",
                "partnership",
                "regulatory",
                "other",
            )
            if event_type not in valid_types:
                return f"Invalid event type: {event_type}. Must be one of: {', '.join(valid_types)}."
            if impact not in ("high", "medium", "low"):
                return f"Invalid impact level: {impact}. Must be high, medium, or low."
            event = mw.store.add_event(competitor, event_type, title, description, source, date, impact)
            logger.info("Added intel event %d: %s for %s", event.event_id, event_type, competitor)
            return f"Event #{event.event_id} added: [{impact}] {title} ({event_type}) for {event.competitor} on {date}."

        def competitor_briefing(
            runtime: ToolRuntime[None, CompetitiveIntelState],
            ticker: Annotated[str, "Ticker to get briefing for (empty for all)"] = "",
        ) -> str:
            """Generate a competitive intelligence briefing."""
            return mw.store.format_briefing(ticker or None)

        def intel_timeline(
            runtime: ToolRuntime[None, CompetitiveIntelState],
            ticker: Annotated[str, "Filter by competitor ticker (empty for all)"] = "",
        ) -> str:
            """Get a chronological timeline of intel events."""
            events = mw.store.events
            if ticker:
                events = [e for e in events if e.competitor == ticker.upper()]
            if not events:
                return "No intel events recorded."
            sorted_events = sorted(events, key=lambda e: e.date)
            lines = ["# Intel Timeline", ""]
            for e in sorted_events:
                impact_marker = {"high": "!!!", "medium": "!!", "low": "!"}.get(e.impact, "!")
                lines.append(f"- {e.date} [{impact_marker}] {e.competitor}: {e.title} ({e.event_type})")
                lines.append(f"    {e.description}")
            return "\n".join(lines)

        def clear_intel(
            runtime: ToolRuntime[None, CompetitiveIntelState],
        ) -> str:
            """Clear all competitive intelligence data."""
            comp_count = len(mw.store.competitors)
            event_count = len(mw.store.events)
            mw.store.competitors.clear()
            mw.store.events.clear()
            mw.store._next_event_id = 1
            logger.info("Cleared %d competitors and %d events", comp_count, event_count)
            return f"Cleared {comp_count} competitor(s) and {event_count} event(s)."

        return [
            StructuredTool.from_function(
                func=add_competitor,
                name="add_competitor",
                description="Add a competitor profile to track.",
            ),
            StructuredTool.from_function(
                func=add_intel_event,
                name="add_intel_event",
                description="Add a competitive intelligence event.",
            ),
            StructuredTool.from_function(
                func=competitor_briefing,
                name="competitor_briefing",
                description="Generate a competitive intelligence briefing.",
            ),
            StructuredTool.from_function(
                func=intel_timeline,
                name="intel_timeline",
                description="Get a chronological timeline of intel events.",
            ),
            StructuredTool.from_function(
                func=clear_intel,
                name="clear_intel",
                description="Clear all competitive intelligence data.",
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append competitive intelligence system prompt to the request."""
        return request.override(
            system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT),
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Wrap synchronous model call with competitive intelligence context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Wrap asynchronous model call with competitive intelligence context."""
        return await call_next(self.modify_request(request))


__all__ = [
    "CompetitiveIntelMiddleware",
    "CompetitorProfile",
    "IntelEvent",
    "IntelStore",
]
