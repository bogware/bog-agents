"""Dashboard mode middleware.

Feature #26: Web-based dashboard for research tasks with configurable
layouts, widgets, and preview capabilities.

## Tools

- `create_layout`: Create a new dashboard layout
- `add_widget`: Add a widget to a dashboard layout
- `list_layouts`: List all dashboard layouts
- `dashboard_preview`: Preview a dashboard layout as text
- `clear_dashboards`: Clear all dashboard layouts

## Usage

```python
from bog_agents.middleware.dashboard import DashboardMiddleware

middleware = DashboardMiddleware()
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

WIDGET_TYPES = ("chart", "table", "text", "metric", "alert", "timeline")


@dataclass
class DashboardWidget:
    """A single dashboard widget.

    Attributes:
        widget_id: Unique widget identifier.
        title: Widget title.
        widget_type: Type of widget (chart, table, text, metric, alert, timeline).
        content: Widget content or data.
        position: Display position order.
        refresh_interval: Auto-refresh interval in seconds.
    """

    widget_id: str
    title: str
    widget_type: str
    content: str = ""
    position: int = 0
    refresh_interval: int = 0


@dataclass
class DashboardLayout:
    """A dashboard layout containing multiple widgets.

    Attributes:
        name: Layout name.
        widgets: List of widgets in this layout.
        created_at: ISO timestamp of creation.
    """

    name: str
    widgets: list[DashboardWidget] = field(default_factory=list)
    created_at: str = ""


@dataclass
class DashboardStore:
    """In-memory store for dashboard layouts.

    Attributes:
        layouts: Dashboard layouts keyed by name.
        active_layout: Name of the currently active layout.
        _next_widget_id: Counter for generating widget IDs.
    """

    layouts: dict[str, DashboardLayout] = field(default_factory=dict)
    active_layout: str = ""
    _next_widget_id: int = 1

    def create_layout(self, name: str) -> DashboardLayout:
        """Create a new dashboard layout.

        Args:
            name: Layout name.

        Returns:
            The created layout.
        """
        layout = DashboardLayout(
            name=name,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.layouts[name] = layout
        if not self.active_layout:
            self.active_layout = name
        return layout

    def add_widget(
        self,
        layout_name: str,
        title: str,
        widget_type: str,
        content: str = "",
        position: int = 0,
        refresh_interval: int = 0,
    ) -> DashboardWidget | None:
        """Add a widget to a layout.

        Args:
            layout_name: Target layout name.
            title: Widget title.
            widget_type: Widget type.
            content: Widget content.
            position: Display position.
            refresh_interval: Refresh interval in seconds.

        Returns:
            The created widget, or None if layout not found.
        """
        layout = self.layouts.get(layout_name)
        if not layout:
            return None
        widget_id = f"w-{self._next_widget_id}"
        self._next_widget_id += 1
        widget = DashboardWidget(
            widget_id=widget_id,
            title=title,
            widget_type=widget_type,
            content=content,
            position=position if position > 0 else len(layout.widgets) + 1,
            refresh_interval=refresh_interval,
        )
        layout.widgets.append(widget)
        layout.widgets.sort(key=lambda w: w.position)
        return widget

    def format_preview(self, layout_name: str) -> str:
        """Format a text preview of a dashboard layout.

        Args:
            layout_name: Layout to preview.

        Returns:
            Formatted preview string.
        """
        layout = self.layouts.get(layout_name)
        if not layout:
            return f"Layout '{layout_name}' not found."
        if not layout.widgets:
            return f"Layout '{layout_name}' has no widgets."

        lines = [
            f"## Dashboard: {layout.name}",
            f"Created: {layout.created_at} | Widgets: {len(layout.widgets)}",
            "",
        ]
        type_icons = {
            "chart": "[CHART]",
            "table": "[TABLE]",
            "text": "[TEXT]",
            "metric": "[METRIC]",
            "alert": "[ALERT]",
            "timeline": "[TIMELINE]",
        }
        for widget in layout.widgets:
            icon = type_icons.get(widget.widget_type, "[?]")
            refresh = f" (refresh: {widget.refresh_interval}s)" if widget.refresh_interval > 0 else ""
            lines.append(f"### {icon} {widget.title}{refresh}")
            lines.append(f"    ID: {widget.widget_id} | Position: {widget.position}")
            if widget.content:
                lines.append(f"    {widget.content}")
            lines.append("")

        return "\n".join(lines)


DASHBOARD_SYSTEM_PROMPT = """## Dashboard Mode Tools

You have access to dashboard creation and management tools for building
web-based research dashboards.

**Available Tools:**
- `create_layout`: Create a new dashboard layout
- `add_widget`: Add widgets (chart, table, text, metric, alert, timeline)
- `list_layouts`: View all available layouts
- `dashboard_preview`: Preview a dashboard as formatted text
- `clear_dashboards`: Remove all layouts

**Widget Types:**
- `chart`: Data visualizations and graphs
- `table`: Tabular data displays
- `text`: Free-form text content
- `metric`: Key performance indicators
- `alert`: Warning and notification panels
- `timeline`: Chronological event displays

**Workflow:**
1. Create a layout with `create_layout`
2. Add widgets using `add_widget`
3. Preview with `dashboard_preview`"""


class DashboardState(TypedDict):
    """State for dashboard middleware."""


class DashboardMiddleware(AgentMiddleware[DashboardState, ContextT, ResponseT]):
    """Middleware for web-based dashboard creation and management.

    Provides tools for creating dashboard layouts, adding configurable
    widgets, and previewing dashboards as formatted text.
    """

    state_schema = DashboardState

    def __init__(self) -> None:
        self.store = DashboardStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build dashboard tools."""
        mw = self

        def create_layout(
            runtime: ToolRuntime[None, DashboardState],
            name: Annotated[str, "Dashboard layout name"],
        ) -> str:
            """Create a new dashboard layout."""
            if name in mw.store.layouts:
                return f"Layout '{name}' already exists. Use a different name."
            layout = mw.store.create_layout(name=name)
            return f"Created dashboard layout '{layout.name}'. Active layout: {mw.store.active_layout}"

        def add_widget(
            runtime: ToolRuntime[None, DashboardState],
            layout_name: Annotated[str, "Target layout name"],
            title: Annotated[str, "Widget title"],
            widget_type: Annotated[str, "Widget type: chart, table, text, metric, alert, timeline"],
            content: Annotated[str, "Widget content or data"] = "",
            position: Annotated[int, "Display position order"] = 0,
            refresh_interval: Annotated[int, "Auto-refresh interval in seconds"] = 0,
        ) -> str:
            """Add a widget to a dashboard layout."""
            if widget_type not in WIDGET_TYPES:
                return f"Invalid widget type '{widget_type}'. Valid types: {', '.join(WIDGET_TYPES)}"
            widget = mw.store.add_widget(
                layout_name=layout_name,
                title=title,
                widget_type=widget_type,
                content=content,
                position=position,
                refresh_interval=refresh_interval,
            )
            if not widget:
                return f"Layout '{layout_name}' not found. Create it first with `create_layout`."
            layout = mw.store.layouts[layout_name]
            return f"Added {widget.widget_type} widget '{widget.title}' ({widget.widget_id}) to '{layout_name}'. Total widgets: {len(layout.widgets)}"

        def list_layouts(
            runtime: ToolRuntime[None, DashboardState],
        ) -> str:
            """List all dashboard layouts."""
            if not mw.store.layouts:
                return "No dashboard layouts created."
            lines = [f"## Dashboard Layouts ({len(mw.store.layouts)})", ""]
            for layout in mw.store.layouts.values():
                active = " **[ACTIVE]**" if layout.name == mw.store.active_layout else ""
                lines.append(f"- **{layout.name}**{active}: {len(layout.widgets)} widgets (created: {layout.created_at})")
            return "\n".join(lines)

        def dashboard_preview(
            runtime: ToolRuntime[None, DashboardState],
            layout_name: Annotated[str, "Layout name to preview"] = "",
        ) -> str:
            """Preview a dashboard layout as formatted text."""
            name = layout_name or mw.store.active_layout
            if not name:
                return "No layout specified and no active layout set."
            return mw.store.format_preview(name)

        def clear_dashboards(
            runtime: ToolRuntime[None, DashboardState],
        ) -> str:
            """Clear all dashboard layouts."""
            count = len(mw.store.layouts)
            mw.store = DashboardStore()
            return f"Cleared {count} dashboard layout(s)."

        return [
            StructuredTool.from_function(name="create_layout", description="Create a new dashboard layout.", func=create_layout),
            StructuredTool.from_function(name="add_widget", description="Add a widget to a dashboard layout.", func=add_widget),
            StructuredTool.from_function(name="list_layouts", description="List all dashboard layouts with widget counts.", func=list_layouts),
            StructuredTool.from_function(
                name="dashboard_preview", description="Preview a dashboard layout as formatted text.", func=dashboard_preview
            ),
            StructuredTool.from_function(name="clear_dashboards", description="Clear all dashboard layouts and widgets.", func=clear_dashboards),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject dashboard instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, DASHBOARD_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject dashboard instructions.

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


__all__ = ["DashboardLayout", "DashboardMiddleware", "DashboardStore", "DashboardWidget"]
