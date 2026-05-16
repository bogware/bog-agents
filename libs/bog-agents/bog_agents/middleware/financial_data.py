"""Financial data connectors middleware.
Feature #11: Framework for registering and tracking financial data sources
(Bloomberg, Refinitiv, Yahoo Finance, FRED) with structured quote and
time series data models.

## Tools

- `register_data_source`: Register a financial data source
- `fetch_quote`: Fetch a quote for a ticker from a registered source
- `fetch_time_series`: Fetch historical time series data
- `list_data_sources`: List registered data sources
- `clear_data_sources`: Clear all sources

## Usage

```python
from bog_agents.middleware.financial_data import FinancialDataMiddleware

middleware = FinancialDataMiddleware()
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
class DataSource:
    """A registered financial data source.

    Attributes:
        name: Display name of the data source.
        source_type: Type of source (bloomberg, refinitiv, yahoo, fred, custom).
        base_url: Base URL for API access.
        api_key_env: Environment variable name for the API key.
        is_connected: Whether the source is currently connected.
    """

    name: str
    source_type: str = "custom"
    base_url: str = ""
    api_key_env: str = ""
    is_connected: bool = False


@dataclass
class QuoteData:
    """A financial quote.

    Attributes:
        ticker: Security ticker symbol.
        price: Current price.
        change: Absolute price change.
        change_pct: Percentage price change.
        volume: Trading volume.
        timestamp: Quote timestamp.
        source: Data source name.
    """

    ticker: str
    price: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    volume: int = 0
    timestamp: str = ""
    source: str = ""


@dataclass
class TimeSeriesPoint:
    """A single point in a time series.

    Attributes:
        date: Date string (YYYY-MM-DD).
        open: Opening price.
        high: High price.
        low: Low price.
        close: Closing price.
        volume: Trading volume.
    """

    date: str
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0


@dataclass
class DataConnectorRegistry:
    """Registry of financial data sources.

    Attributes:
        sources: Dictionary of registered data sources keyed by name.
    """

    sources: dict[str, DataSource] = field(default_factory=dict)

    def register(self, source: DataSource) -> None:
        """Register a data source.

        Args:
            source: The data source to register.
        """
        self.sources[source.name] = source

    def get(self, name: str) -> DataSource | None:
        """Get a data source by name.

        Args:
            name: Name of the data source.

        Returns:
            The data source, or None if not found.
        """
        return self.sources.get(name)

    def format_sources(self) -> str:
        """Format registered sources as markdown.

        Returns:
            Markdown-formatted list of data sources.
        """
        if not self.sources:
            return "No data sources registered."

        lines = ["## Registered Data Sources", ""]
        for name, src in self.sources.items():
            status = "Connected" if src.is_connected else "Disconnected"
            lines.append(f"- **{name}** ({src.source_type}): {src.base_url or 'N/A'} [{status}]")
            if src.api_key_env:
                lines.append(f"  API Key Env: `{src.api_key_env}`")
        return "\n".join(lines)


FINANCIAL_DATA_SYSTEM_PROMPT = """## Financial Data Connectors

You have tools to register and query financial data sources.

**Supported Source Types:** bloomberg, refinitiv, yahoo, fred, custom

**Workflow:**
1. `register_data_source` — Register a data source with connection details
2. `fetch_quote` — Fetch current quote for a ticker from a registered source
3. `fetch_time_series` — Fetch historical OHLCV data
4. `list_data_sources` — View all registered sources

**Note:** This is a registry/framework. Actual API calls use the registered
connection info. The tools provide structure, tracking, and data models."""


class FinancialDataState(TypedDict):
    """State for financial data middleware."""


class FinancialDataMiddleware(AgentMiddleware[FinancialDataState, ContextT, ResponseT]):
    """Middleware for financial data source registration and tracking."""

    state_schema = FinancialDataState

    def __init__(self) -> None:
        self.registry = DataConnectorRegistry()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build financial data tools."""
        mw = self

        def register_data_source(
            runtime: ToolRuntime[None, FinancialDataState],
            name: Annotated[str, "Display name for the data source"],
            source_type: Annotated[str, "Source type: bloomberg, refinitiv, yahoo, fred, custom"] = "custom",
            base_url: Annotated[str, "Base URL for API access"] = "",
            api_key_env: Annotated[str, "Environment variable name for the API key"] = "",
        ) -> str:
            """Register a financial data source."""
            source = DataSource(
                name=name,
                source_type=source_type,
                base_url=base_url,
                api_key_env=api_key_env,
                is_connected=True,
            )
            mw.registry.register(source)
            return f"Data source registered: {name} ({source_type}). Total sources: {len(mw.registry.sources)}"

        def fetch_quote(
            runtime: ToolRuntime[None, FinancialDataState],
            ticker: Annotated[str, "Ticker symbol to fetch"],
            source_name: Annotated[str, "Name of the registered data source"],
        ) -> str:
            """Fetch a quote for a ticker from a registered source."""
            source = mw.registry.get(source_name)
            if not source:
                return f"Data source '{source_name}' not found. Use `register_data_source` first."
            quote = QuoteData(
                ticker=ticker,
                price=0.0,
                change=0.0,
                change_pct=0.0,
                volume=0,
                timestamp="pending",
                source=source_name,
            )
            lines = [
                f"## Quote: {ticker} (via {source_name})",
                f"  Source Type: {source.source_type}",
                f"  Base URL: {source.base_url or 'N/A'}",
                f"  Price: ${quote.price:,.2f}",
                f"  Change: ${quote.change:,.2f} ({quote.change_pct:,.2f}%)",
                f"  Volume: {quote.volume:,}",
                f"  Timestamp: {quote.timestamp}",
                "",
                "Note: Populate with actual data from the source API.",
            ]
            return "\n".join(lines)

        def fetch_time_series(
            runtime: ToolRuntime[None, FinancialDataState],
            ticker: Annotated[str, "Ticker symbol"],
            source_name: Annotated[str, "Name of the registered data source"],
            start_date: Annotated[str, "Start date (YYYY-MM-DD)"] = "",
            end_date: Annotated[str, "End date (YYYY-MM-DD)"] = "",
        ) -> str:
            """Fetch historical time series data."""
            source = mw.registry.get(source_name)
            if not source:
                return f"Data source '{source_name}' not found. Use `register_data_source` first."
            lines = [
                f"## Time Series: {ticker} (via {source_name})",
                f"  Source Type: {source.source_type}",
                f"  Period: {start_date or 'unspecified'} to {end_date or 'unspecified'}",
                f"  Base URL: {source.base_url or 'N/A'}",
                "",
                "| Date | Open | High | Low | Close | Volume |",
                "| --- | --- | --- | --- | --- | --- |",
                "| (populate from API) | | | | | |",
                "",
                "Note: Use the registered source connection to fetch actual OHLCV data.",
            ]
            return "\n".join(lines)

        def list_data_sources(
            runtime: ToolRuntime[None, FinancialDataState],
        ) -> str:
            """List registered data sources."""
            return mw.registry.format_sources()

        def clear_data_sources(
            runtime: ToolRuntime[None, FinancialDataState],
        ) -> str:
            """Clear all registered data sources."""
            mw.registry = DataConnectorRegistry()
            return "All data sources cleared."

        return [
            StructuredTool.from_function(
                name="register_data_source", description="Register a financial data source with connection details.", func=register_data_source
            ),
            StructuredTool.from_function(name="fetch_quote", description="Fetch a quote for a ticker from a registered source.", func=fetch_quote),
            StructuredTool.from_function(
                name="fetch_time_series", description="Fetch historical time series data from a registered source.", func=fetch_time_series
            ),
            StructuredTool.from_function(name="list_data_sources", description="List all registered data sources.", func=list_data_sources),
            StructuredTool.from_function(name="clear_data_sources", description="Clear all registered data sources.", func=clear_data_sources),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject financial data instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, FINANCIAL_DATA_SYSTEM_PROMPT))

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


__all__ = ["DataConnectorRegistry", "DataSource", "FinancialDataMiddleware", "QuoteData", "TimeSeriesPoint"]
