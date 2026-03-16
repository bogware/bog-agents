"""Market Sentiment Dashboard middleware for social/news/options sentiment aggregation."""

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
class SentimentSignal:
    """A single sentiment signal from a data source."""

    signal_id: int
    source: str  # social_media, news, analyst, options_flow, insider, custom
    ticker: str
    sentiment: float  # -1.0 (bearish) to 1.0 (bullish)
    confidence: float
    category: str  # bullish, bearish, neutral
    timestamp: str


@dataclass
class SentimentAggregation:
    """Aggregated sentiment data for a ticker."""

    ticker: str
    avg_sentiment: float
    signal_count: int
    sources: list[str]
    bullish_pct: float
    bearish_pct: float


@dataclass
class SentimentStore:
    """Storage for sentiment signals."""

    signals: list[SentimentSignal] = field(default_factory=list)
    _next_id: int = 1

    def add_signal(
        self,
        source: str,
        ticker: str,
        sentiment: float,
        confidence: float,
        category: str,
    ) -> SentimentSignal:
        """Add a new sentiment signal."""
        signal = SentimentSignal(
            signal_id=self._next_id,
            source=source,
            ticker=ticker.upper(),
            sentiment=sentiment,
            confidence=confidence,
            category=category,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self._next_id += 1
        self.signals.append(signal)
        return signal

    def aggregate(self, ticker: str | None = None) -> list[SentimentAggregation]:
        """Compute per-ticker sentiment averages."""
        tickers: dict[str, list[SentimentSignal]] = {}
        for s in self.signals:
            if ticker and s.ticker != ticker.upper():
                continue
            tickers.setdefault(s.ticker, []).append(s)

        results = []
        for tk, sigs in sorted(tickers.items()):
            total = len(sigs)
            avg = sum(s.sentiment for s in sigs) / total
            sources = sorted({s.source for s in sigs})
            bullish = sum(1 for s in sigs if s.category == "bullish")
            bearish = sum(1 for s in sigs if s.category == "bearish")
            results.append(
                SentimentAggregation(
                    ticker=tk,
                    avg_sentiment=round(avg, 4),
                    signal_count=total,
                    sources=sources,
                    bullish_pct=round(bullish / total * 100, 1),
                    bearish_pct=round(bearish / total * 100, 1),
                )
            )
        return results

    def format_dashboard(self) -> str:
        """Format a sentiment dashboard summary."""
        aggs = self.aggregate()
        if not aggs:
            return "No sentiment data available."
        lines = ["# Market Sentiment Dashboard", ""]
        for a in aggs:
            bar = "+" * int(abs(a.avg_sentiment) * 10) if a.avg_sentiment >= 0 else "-" * int(abs(a.avg_sentiment) * 10)
            direction = "bullish" if a.avg_sentiment > 0.1 else ("bearish" if a.avg_sentiment < -0.1 else "neutral")
            lines.append(
                f"**{a.ticker}** [{bar}] {a.avg_sentiment:+.2f} ({direction}) — "
                f"{a.signal_count} signals, {a.bullish_pct:.0f}% bull / {a.bearish_pct:.0f}% bear"
            )
            lines.append(f"  Sources: {', '.join(a.sources)}")
        return "\n".join(lines)


SYSTEM_PROMPT = """You have access to market sentiment tools for aggregating signals from social media, \
news, analysts, options flow, and insiders. Sentiment values range from -1.0 (bearish) to 1.0 (bullish). \
Categories: bullish, bearish, neutral. Sources: social_media, news, analyst, options_flow, insider, custom."""


class MarketSentimentState(TypedDict):
    """State for market sentiment middleware."""


class MarketSentimentMiddleware(AgentMiddleware[MarketSentimentState, ContextT, ResponseT]):
    """Middleware providing market sentiment aggregation and dashboard tools."""

    state_schema = MarketSentimentState

    def __init__(self) -> None:
        self.store = SentimentStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build the market sentiment tools."""
        mw = self

        def add_sentiment_signal(
            runtime: ToolRuntime[None, MarketSentimentState],
            ticker: Annotated[str, "Stock ticker symbol"],
            source: Annotated[str, "Source: social_media, news, analyst, options_flow, insider, or custom"],
            sentiment: Annotated[float, "Sentiment score from -1.0 (bearish) to 1.0 (bullish)"],
            confidence: Annotated[float, "Confidence score between 0.0 and 1.0"],
            category: Annotated[str, "Category: bullish, bearish, or neutral"],
        ) -> str:
            """Add a sentiment signal for a ticker."""
            if source not in ("social_media", "news", "analyst", "options_flow", "insider", "custom"):
                return f"Invalid source: {source}. Must be social_media, news, analyst, options_flow, insider, or custom."
            if not -1.0 <= sentiment <= 1.0:
                return "Sentiment must be between -1.0 and 1.0."
            if not 0.0 <= confidence <= 1.0:
                return "Confidence must be between 0.0 and 1.0."
            if category not in ("bullish", "bearish", "neutral"):
                return f"Invalid category: {category}. Must be bullish, bearish, or neutral."
            signal = mw.store.add_signal(source, ticker, sentiment, confidence, category)
            logger.info("Added sentiment signal %d: %s %s %.2f", signal.signal_id, ticker, source, sentiment)
            return (
                f"Signal #{signal.signal_id} added: {signal.ticker} [{source}] sentiment={sentiment:+.2f} ({category}, confidence={confidence:.2f})."
            )

        def sentiment_dashboard(
            runtime: ToolRuntime[None, MarketSentimentState],
        ) -> str:
            """Display the market sentiment dashboard with all tickers."""
            return mw.store.format_dashboard()

        def ticker_sentiment(
            runtime: ToolRuntime[None, MarketSentimentState],
            ticker: Annotated[str, "Stock ticker symbol to query"],
        ) -> str:
            """Get aggregated sentiment for a specific ticker."""
            aggs = mw.store.aggregate(ticker)
            if not aggs:
                return f"No sentiment data for {ticker.upper()}."
            a = aggs[0]
            lines = [
                f"# Sentiment: {a.ticker}",
                f"Average Sentiment: {a.avg_sentiment:+.4f}",
                f"Signal Count: {a.signal_count}",
                f"Bullish: {a.bullish_pct:.1f}% | Bearish: {a.bearish_pct:.1f}%",
                f"Sources: {', '.join(a.sources)}",
            ]
            return "\n".join(lines)

        def list_signals(
            runtime: ToolRuntime[None, MarketSentimentState],
            ticker: Annotated[str, "Filter by ticker (empty for all)"] = "",
        ) -> str:
            """List all sentiment signals, optionally filtered by ticker."""
            signals = mw.store.signals
            if ticker:
                signals = [s for s in signals if s.ticker == ticker.upper()]
            if not signals:
                return "No sentiment signals found."
            lines = ["# Sentiment Signals", ""]
            for s in signals:
                lines.append(f"- #{s.signal_id} {s.ticker} [{s.source}] {s.sentiment:+.2f} ({s.category}, conf={s.confidence:.2f}) at {s.timestamp}")
            return "\n".join(lines)

        def clear_sentiment(
            runtime: ToolRuntime[None, MarketSentimentState],
        ) -> str:
            """Clear all sentiment signals."""
            count = len(mw.store.signals)
            mw.store.signals.clear()
            mw.store._next_id = 1
            logger.info("Cleared %d sentiment signals", count)
            return f"Cleared {count} sentiment signal(s)."

        return [
            StructuredTool.from_function(
                func=add_sentiment_signal,
                name="add_sentiment_signal",
                description="Add a sentiment signal for a ticker.",
            ),
            StructuredTool.from_function(
                func=sentiment_dashboard,
                name="sentiment_dashboard",
                description="Display the market sentiment dashboard.",
            ),
            StructuredTool.from_function(
                func=ticker_sentiment,
                name="ticker_sentiment",
                description="Get aggregated sentiment for a specific ticker.",
            ),
            StructuredTool.from_function(
                func=list_signals,
                name="list_signals",
                description="List all sentiment signals.",
            ),
            StructuredTool.from_function(
                func=clear_sentiment,
                name="clear_sentiment",
                description="Clear all sentiment signals.",
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append market sentiment system prompt to the request."""
        return request.override(
            system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT),
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Wrap synchronous model call with market sentiment context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Wrap asynchronous model call with market sentiment context."""
        return await call_next(self.modify_request(request))


__all__ = [
    "MarketSentimentMiddleware",
    "SentimentAggregation",
    "SentimentSignal",
    "SentimentStore",
]
