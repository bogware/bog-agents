"""Scheduled reports middleware.

Feature #27: Cron-like scheduling for recurring reports with support
for multiple report types, recipient management, and run tracking.

## Tools

- `create_schedule`: Create a new report schedule
- `list_schedules`: List all report schedules
- `toggle_schedule`: Enable or disable a schedule
- `run_report_now`: Immediately run a scheduled report
- `clear_schedules`: Clear all report schedules

## Usage

```python
from bog_agents.middleware.scheduled_reports import ScheduledReportsMiddleware

middleware = ScheduledReportsMiddleware()
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

REPORT_TYPES = ("portfolio_summary", "performance", "compliance", "risk", "custom")


@dataclass
class ReportSchedule:
    """A scheduled report configuration.

    Attributes:
        schedule_id: Unique schedule identifier.
        name: Human-readable schedule name.
        report_type: Type of report (portfolio_summary, performance, compliance, risk, custom).
        cron_expression: Cron-like schedule expression.
        recipients: List of recipient email addresses or identifiers.
        last_run: ISO timestamp of the last run.
        next_run: ISO timestamp of the next scheduled run.
        is_active: Whether the schedule is active.
        run_count: Number of times the report has been run.
    """

    schedule_id: str
    name: str
    report_type: str
    cron_expression: str
    recipients: list[str] = field(default_factory=list)
    last_run: str = ""
    next_run: str = ""
    is_active: bool = True
    run_count: int = 0


@dataclass
class ScheduledReportStore:
    """In-memory store for report schedules.

    Attributes:
        schedules: List of report schedules.
        _next_id: Counter for generating schedule IDs.
    """

    schedules: list[ReportSchedule] = field(default_factory=list)
    _next_id: int = 1

    def add_schedule(
        self,
        name: str,
        report_type: str,
        cron_expression: str,
        recipients: list[str] | None = None,
    ) -> ReportSchedule:
        """Add a new report schedule.

        Args:
            name: Schedule name.
            report_type: Report type.
            cron_expression: Cron-like expression.
            recipients: List of recipients.

        Returns:
            The created schedule.
        """
        schedule_id = f"sched-{self._next_id}"
        self._next_id += 1
        schedule = ReportSchedule(
            schedule_id=schedule_id,
            name=name,
            report_type=report_type,
            cron_expression=cron_expression,
            recipients=recipients or [],
            next_run=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            is_active=True,
        )
        self.schedules.append(schedule)
        return schedule

    def get(self, schedule_id: str) -> ReportSchedule | None:
        """Get a schedule by ID.

        Args:
            schedule_id: Schedule identifier.

        Returns:
            The schedule, or None if not found.
        """
        for schedule in self.schedules:
            if schedule.schedule_id == schedule_id:
                return schedule
        return None

    def toggle(self, schedule_id: str) -> ReportSchedule | None:
        """Toggle a schedule's active state.

        Args:
            schedule_id: Schedule identifier.

        Returns:
            The updated schedule, or None if not found.
        """
        schedule = self.get(schedule_id)
        if schedule:
            schedule.is_active = not schedule.is_active
        return schedule

    def record_run(self, schedule_id: str) -> ReportSchedule | None:
        """Record that a scheduled report was run.

        Args:
            schedule_id: Schedule identifier.

        Returns:
            The updated schedule, or None if not found.
        """
        schedule = self.get(schedule_id)
        if schedule:
            schedule.last_run = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())
            schedule.run_count += 1
        return schedule

    def format_listing(self) -> str:
        """Format all schedules for display.

        Returns:
            Formatted listing string.
        """
        if not self.schedules:
            return "No report schedules configured."

        lines = [
            f"## Scheduled Reports ({len(self.schedules)})",
            "",
        ]
        active = [s for s in self.schedules if s.is_active]
        inactive = [s for s in self.schedules if not s.is_active]

        if active:
            lines.append(f"### Active ({len(active)})")
            for schedule in active:
                lines.append(f"  - **{schedule.schedule_id}**: {schedule.name}")
                lines.append(f"    Type: {schedule.report_type} | Cron: {schedule.cron_expression}")
                lines.append(f"    Recipients: {', '.join(schedule.recipients) if schedule.recipients else 'None'}")
                lines.append(f"    Runs: {schedule.run_count} | Last: {schedule.last_run or 'Never'} | Next: {schedule.next_run}")
                lines.append("")

        if inactive:
            lines.append(f"### Inactive ({len(inactive)})")
            for schedule in inactive:
                lines.append(f"  - **{schedule.schedule_id}**: {schedule.name} [PAUSED]")
                lines.append(f"    Type: {schedule.report_type} | Runs: {schedule.run_count}")
                lines.append("")

        return "\n".join(lines)


SCHEDULED_REPORTS_SYSTEM_PROMPT = """## Scheduled Reports Tools

You have access to tools for managing recurring report schedules.

**Available Tools:**
- `create_schedule`: Create a new cron-based report schedule
- `list_schedules`: View all configured schedules
- `toggle_schedule`: Enable or disable a schedule
- `run_report_now`: Trigger an immediate report run
- `clear_schedules`: Remove all schedules

**Report Types:**
- `portfolio_summary`: Overview of portfolio holdings and performance
- `performance`: Detailed performance analysis and attribution
- `compliance`: Regulatory compliance status and violations
- `risk`: Risk metrics, VaR, and exposure analysis
- `custom`: User-defined report template

**Cron Format:**
Use standard cron expressions (e.g., `0 9 * * 1` for every Monday at 9am)."""


class ScheduledReportsState(TypedDict):
    """State for scheduled reports middleware."""


class ScheduledReportsMiddleware(AgentMiddleware[ScheduledReportsState, ContextT, ResponseT]):
    """Middleware for cron-like scheduling of recurring reports.

    Provides tools for creating report schedules, managing recipients,
    toggling active state, and triggering immediate report runs.
    """

    state_schema = ScheduledReportsState

    def __init__(self) -> None:
        self.store = ScheduledReportStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build scheduled reports tools."""
        mw = self

        def create_schedule(
            runtime: ToolRuntime[None, ScheduledReportsState],
            name: Annotated[str, "Schedule name"],
            report_type: Annotated[str, "Report type: portfolio_summary, performance, compliance, risk, custom"],
            cron_expression: Annotated[str, "Cron expression (e.g., '0 9 * * 1' for Monday 9am)"],
            recipients: Annotated[str, "Comma-separated recipient emails or identifiers"] = "",
        ) -> str:
            """Create a new report schedule with cron-based timing."""
            if report_type not in REPORT_TYPES:
                return f"Invalid report type '{report_type}'. Valid types: {', '.join(REPORT_TYPES)}"
            recipient_list = [r.strip() for r in recipients.split(",") if r.strip()] if recipients else []
            schedule = mw.store.add_schedule(
                name=name,
                report_type=report_type,
                cron_expression=cron_expression,
                recipients=recipient_list,
            )
            return f"Created schedule '{schedule.name}' ({schedule.schedule_id}): {schedule.report_type} at '{schedule.cron_expression}' -> {len(schedule.recipients)} recipient(s)"

        def list_schedules(
            runtime: ToolRuntime[None, ScheduledReportsState],
        ) -> str:
            """List all configured report schedules."""
            return mw.store.format_listing()

        def toggle_schedule(
            runtime: ToolRuntime[None, ScheduledReportsState],
            schedule_id: Annotated[str, "Schedule ID to toggle"],
        ) -> str:
            """Enable or disable a report schedule."""
            schedule = mw.store.toggle(schedule_id)
            if not schedule:
                return f"Schedule '{schedule_id}' not found."
            status = "ACTIVE" if schedule.is_active else "PAUSED"
            return f"Schedule '{schedule.name}' ({schedule.schedule_id}) is now {status}."

        def run_report_now(
            runtime: ToolRuntime[None, ScheduledReportsState],
            schedule_id: Annotated[str, "Schedule ID to run immediately"],
        ) -> str:
            """Immediately run a scheduled report."""
            schedule = mw.store.get(schedule_id)
            if not schedule:
                return f"Schedule '{schedule_id}' not found."
            mw.store.record_run(schedule_id)
            lines = [
                f"## Report Run: {schedule.name}",
                f"Type: {schedule.report_type} | Run #{schedule.run_count}",
                f"Executed at: {schedule.last_run}",
                f"Recipients: {', '.join(schedule.recipients) if schedule.recipients else 'None'}",
                "",
                f"Report '{schedule.report_type}' generated successfully.",
            ]
            return "\n".join(lines)

        def clear_schedules(
            runtime: ToolRuntime[None, ScheduledReportsState],
        ) -> str:
            """Clear all report schedules."""
            count = len(mw.store.schedules)
            mw.store = ScheduledReportStore()
            return f"Cleared {count} report schedule(s)."

        return [
            StructuredTool.from_function(
                name="create_schedule", description="Create a new cron-based report schedule with recipients.", func=create_schedule
            ),
            StructuredTool.from_function(name="list_schedules", description="List all configured report schedules with status.", func=list_schedules),
            StructuredTool.from_function(name="toggle_schedule", description="Enable or disable a report schedule.", func=toggle_schedule),
            StructuredTool.from_function(name="run_report_now", description="Immediately trigger a scheduled report run.", func=run_report_now),
            StructuredTool.from_function(name="clear_schedules", description="Clear all report schedules.", func=clear_schedules),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject scheduled reports instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, SCHEDULED_REPORTS_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject scheduled reports instructions.

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


__all__ = ["ReportSchedule", "ScheduledReportStore", "ScheduledReportsMiddleware"]
