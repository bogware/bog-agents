"""Natural language to SQL/analytics query middleware.

Feature #43: Financial advisors describe queries in plain English, the agent
translates to structured queries over portfolio data.

## Tools

- `register_dataset`: Register a dataset schema (columns, types)
- `nl_query`: Translate natural language to a structured query
- `run_query`: Execute a query against registered data
- `list_datasets`: Show available datasets

## Usage

```python
from bog_agents.middleware.nl_query import NLQueryMiddleware

middleware = NLQueryMiddleware()
```
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

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
class DatasetColumn:
    """A column in a dataset."""

    name: str
    dtype: str = "text"
    description: str = ""


@dataclass
class Dataset:
    """A registered dataset schema."""

    name: str
    columns: list[DatasetColumn] = field(default_factory=list)
    description: str = ""
    row_count: int = 0
    data: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QueryResult:
    """Result of a structured query."""

    query: str
    dataset: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str = ""

    def format_table(self, max_rows: int = 20) -> str:
        """Format results as a Markdown table."""
        if self.error:
            return f"Error: {self.error}"
        if not self.rows:
            return "No results found."

        cols = self.columns or (list(self.rows[0].keys()) if self.rows else [])
        lines = [f"## Query Results ({len(self.rows)} rows)", f"Query: {self.query}", ""]

        # Header
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("| " + " | ".join("---" for _ in cols) + " |")

        # Rows
        for row in self.rows[:max_rows]:
            values = [str(row.get(c, "")) for c in cols]
            lines.append("| " + " | ".join(values) + " |")

        if len(self.rows) > max_rows:
            lines.append(f"\n*Showing {max_rows} of {len(self.rows)} rows*")

        return "\n".join(lines)


@dataclass
class DataStore:
    """In-memory data store for analytics queries."""

    datasets: dict[str, Dataset] = field(default_factory=dict)

    def register(self, name: str, columns: list[DatasetColumn], description: str = "") -> Dataset:
        """Register a dataset schema."""
        ds = Dataset(name=name, columns=columns, description=description)
        self.datasets[name] = ds
        return ds

    def add_rows(self, name: str, rows: list[dict[str, Any]]) -> int:
        """Add rows to a dataset."""
        ds = self.datasets.get(name)
        if not ds:
            return 0
        ds.data.extend(rows)
        ds.row_count = len(ds.data)
        return len(rows)

    def query(
        self, dataset_name: str, *, filters: dict[str, Any] | None = None, sort_by: str = "", limit: int = 20, columns: list[str] | None = None
    ) -> QueryResult:
        """Execute a simple query against a dataset.

        Args:
            dataset_name: Dataset to query.
            filters: Column=value filters (exact match).
            sort_by: Column to sort by (prefix with - for descending).
            limit: Maximum rows to return.
            columns: Columns to include in output.

        Returns:
            Query result.
        """
        ds = self.datasets.get(dataset_name)
        if not ds:
            return QueryResult(query="", dataset=dataset_name, error=f"Dataset '{dataset_name}' not found.")

        rows = list(ds.data)

        # Apply filters
        if filters:
            for col, val in filters.items():
                rows = [r for r in rows if str(r.get(col, "")) == str(val)]

        # Sort
        if sort_by:
            desc = sort_by.startswith("-")
            col = sort_by.lstrip("-")
            rows.sort(key=lambda r: r.get(col, ""), reverse=desc)

        # Limit
        rows = rows[:limit]

        # Select columns
        out_cols = columns or [c.name for c in ds.columns]

        return QueryResult(
            query=f"SELECT FROM {dataset_name}",
            dataset=dataset_name,
            rows=rows,
            columns=out_cols,
        )

    def format_datasets(self) -> str:
        """List all registered datasets."""
        if not self.datasets:
            return "No datasets registered."

        lines = ["## Available Datasets", ""]
        for ds in self.datasets.values():
            lines.append(f"### {ds.name}")
            if ds.description:
                lines.append(f"{ds.description}")
            lines.append(f"Rows: {ds.row_count}")
            lines.append("Columns:")
            for col in ds.columns:
                lines.append(f"  - `{col.name}` ({col.dtype}): {col.description}")
            lines.append("")
        return "\n".join(lines)


NL_QUERY_SYSTEM_PROMPT = """## Natural Language Analytics

You can translate natural language questions into structured queries over registered datasets.

**Workflow:**
1. Use `register_dataset` to define available data schemas
2. Use `add_data_rows` to populate datasets
3. Use `query_data` to run structured queries
4. Use `list_datasets` to see available data

**Example Questions:**
- "Show me all clients with more than 40% tech exposure"
- "Which accounts haven't been rebalanced in 6 months?"
- "Compare performance of my model portfolios YTD"

Translate these into appropriate filters, sorts, and column selections."""


class NLQueryState(TypedDict):
    """State for NL query middleware."""


class NLQueryMiddleware(AgentMiddleware[NLQueryState, ContextT, ResponseT]):
    """Middleware for natural language to structured query translation."""

    state_schema = NLQueryState

    def __init__(self) -> None:
        self.store = DataStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build NL query tools."""
        mw = self

        def register_dataset(
            runtime: ToolRuntime[None, NLQueryState],
            name: Annotated[str, "Dataset name"],
            columns: Annotated[str, "Comma-separated column definitions as 'name:type:description'"],
            description: Annotated[str, "Dataset description"] = "",
        ) -> str:
            """Register a dataset schema for querying."""
            cols = []
            for col_def in columns.split(","):
                parts = col_def.strip().split(":")
                col_name = parts[0].strip()
                col_type = parts[1].strip() if len(parts) > 1 else "text"
                col_desc = parts[2].strip() if len(parts) > 2 else ""  # noqa: PLR2004
                cols.append(DatasetColumn(name=col_name, dtype=col_type, description=col_desc))
            ds = mw.store.register(name, cols, description)
            return f"Dataset '{name}' registered with {len(cols)} columns."

        def add_data_rows(
            runtime: ToolRuntime[None, NLQueryState],
            dataset: Annotated[str, "Dataset name"],
            rows: Annotated[str, "Rows as JSON array of objects"],
        ) -> str:
            """Add rows to a registered dataset. Pass rows as JSON array."""
            import json

            try:
                data = json.loads(rows)
            except json.JSONDecodeError as e:
                return f"Invalid JSON: {e}"
            if not isinstance(data, list):
                return "Rows must be a JSON array of objects."
            added = mw.store.add_rows(dataset, data)
            return f"Added {added} rows to '{dataset}'."

        def query_data(
            runtime: ToolRuntime[None, NLQueryState],
            dataset: Annotated[str, "Dataset to query"],
            filters: Annotated[str, "Comma-separated filters as 'column=value'"] = "",
            sort_by: Annotated[str, "Column to sort by (prefix with - for descending)"] = "",
            limit: Annotated[int, "Maximum rows to return"] = 20,
            columns: Annotated[str, "Comma-separated columns to include"] = "",
        ) -> str:
            """Query a dataset with filters, sorting, and column selection."""
            filter_dict = {}
            if filters:
                for f in filters.split(","):
                    if "=" in f:
                        k, v = f.strip().split("=", 1)
                        filter_dict[k.strip()] = v.strip()

            col_list = [c.strip() for c in columns.split(",") if c.strip()] if columns else None

            result = mw.store.query(
                dataset,
                filters=filter_dict or None,
                sort_by=sort_by,
                limit=limit,
                columns=col_list,
            )
            return result.format_table()

        def list_datasets(
            runtime: ToolRuntime[None, NLQueryState],
        ) -> str:
            """List all registered datasets with their schemas."""
            return mw.store.format_datasets()

        return [
            StructuredTool.from_function(name="register_dataset", description="Register a dataset schema for querying.", func=register_dataset),
            StructuredTool.from_function(name="add_data_rows", description="Add rows to a dataset (JSON array).", func=add_data_rows),
            StructuredTool.from_function(
                name="query_data", description="Query a dataset with filters, sorting, and column selection.", func=query_data
            ),
            StructuredTool.from_function(name="list_datasets", description="List all registered datasets.", func=list_datasets),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject NL query instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, NL_QUERY_SYSTEM_PROMPT))

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


__all__ = ["DataStore", "Dataset", "NLQueryMiddleware", "QueryResult"]
