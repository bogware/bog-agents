"""Regulatory alert monitor middleware.
Feature #13: Track SEC rules, FINRA notices, and other regulatory changes
with severity levels, filtering, and review workflows.

## Tools

- `add_alert`: Add a regulatory alert
- `list_alerts`: List all alerts with optional filtering
- `mark_alert_reviewed`: Mark an alert as reviewed with notes
- `alert_summary`: Generate summary of pending alerts
- `clear_alerts`: Clear all alerts

## Usage

```python
from bog_agents.middleware.regulatory_alerts import RegulatoryAlertsMiddleware

middleware = RegulatoryAlertsMiddleware()
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
class RegulatoryAlert:
    """A regulatory alert.

    Attributes:
        alert_id: Unique identifier.
        title: Alert title.
        source: Issuing body or source.
        category: Category (sec, finra, dol, state, other).
        severity: Severity level (critical, high, medium, low).
        affected_entities: List of affected entities.
        date: Date of the alert (YYYY-MM-DD).
        description: Detailed description.
        status: Status (pending, reviewed, dismissed).
        review_notes: Notes from the review.
    """

    alert_id: int
    title: str
    source: str
    category: str = "other"
    severity: str = "medium"
    affected_entities: list[str] = field(default_factory=list)
    date: str = ""
    description: str = ""
    status: str = "pending"
    review_notes: str = ""


@dataclass
class AlertStore:
    """Store of regulatory alerts.

    Attributes:
        alerts: List of all alerts.
    """

    alerts: list[RegulatoryAlert] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def add(self, **kwargs: object) -> RegulatoryAlert:
        """Add a regulatory alert.

        Args:
            **kwargs: Alert fields.

        Returns:
            The created alert.
        """
        alert = RegulatoryAlert(alert_id=self._next_id, **kwargs)
        self.alerts.append(alert)
        self._next_id += 1
        return alert

    def get(self, alert_id: int) -> RegulatoryAlert | None:
        """Get an alert by ID.

        Args:
            alert_id: The alert ID.

        Returns:
            The alert, or None if not found.
        """
        for alert in self.alerts:
            if alert.alert_id == alert_id:
                return alert
        return None

    def filter_alerts(
        self,
        category: str = "",
        severity: str = "",
        status: str = "",
    ) -> list[RegulatoryAlert]:
        """Filter alerts by category, severity, and/or status.

        Args:
            category: Filter by category (empty for all).
            severity: Filter by severity (empty for all).
            status: Filter by status (empty for all).

        Returns:
            Filtered list of alerts.
        """
        result = self.alerts
        if category:
            result = [a for a in result if a.category == category]
        if severity:
            result = [a for a in result if a.severity == severity]
        if status:
            result = [a for a in result if a.status == status]
        return result

    def format_summary(self) -> str:
        """Format summary of pending alerts.

        Returns:
            Markdown-formatted alert summary.
        """
        pending = [a for a in self.alerts if a.status == "pending"]
        if not pending:
            return "No pending regulatory alerts."

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        pending.sort(key=lambda a: severity_order.get(a.severity, 99))

        critical = sum(1 for a in pending if a.severity == "critical")
        high = sum(1 for a in pending if a.severity == "high")
        medium = sum(1 for a in pending if a.severity == "medium")
        low = sum(1 for a in pending if a.severity == "low")

        lines = [
            "## Regulatory Alert Summary",
            f"Pending: {len(pending)} (Critical: {critical}, High: {high}, Medium: {medium}, Low: {low})",
            "",
        ]

        for alert in pending:
            entities = ", ".join(alert.affected_entities) if alert.affected_entities else "N/A"
            lines.append(f"### [{alert.severity.upper()}] {alert.title} (#{alert.alert_id})")
            lines.append(f"  Source: {alert.source} | Category: {alert.category} | Date: {alert.date}")
            lines.append(f"  Affected: {entities}")
            if alert.description:
                lines.append(f"  {alert.description}")
            lines.append("")

        return "\n".join(lines)


REGULATORY_SYSTEM_PROMPT = """## Regulatory Alert Monitor

You have tools to track and manage regulatory alerts.

**Categories:** sec, finra, dol, state, other
**Severity Levels:** critical, high, medium, low
**Statuses:** pending, reviewed, dismissed

**Workflow:**
1. `add_alert` — Add alerts as they are discovered
2. `list_alerts` — View alerts with optional filtering
3. `mark_alert_reviewed` — Mark alerts as reviewed with notes
4. `alert_summary` — Get a summary of all pending alerts

Always prioritize critical and high severity alerts for immediate review."""


class RegulatoryAlertsState(TypedDict):
    """State for regulatory alerts middleware."""


class RegulatoryAlertsMiddleware(AgentMiddleware[RegulatoryAlertsState, ContextT, ResponseT]):
    """Middleware for tracking and managing regulatory alerts."""

    state_schema = RegulatoryAlertsState

    def __init__(self) -> None:
        self.store = AlertStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build regulatory alert tools."""
        mw = self

        def add_alert(
            runtime: ToolRuntime[None, RegulatoryAlertsState],
            title: Annotated[str, "Alert title"],
            source: Annotated[str, "Issuing body or source (e.g., SEC, FINRA)"],
            category: Annotated[str, "Category: sec, finra, dol, state, other"] = "other",
            severity: Annotated[str, "Severity: critical, high, medium, low"] = "medium",
            affected_entities: Annotated[str, "Comma-separated list of affected entities"] = "",
            date: Annotated[str, "Alert date (YYYY-MM-DD)"] = "",
            description: Annotated[str, "Detailed description"] = "",
        ) -> str:
            """Add a regulatory alert."""
            entities = [e.strip() for e in affected_entities.split(",") if e.strip()] if affected_entities else []
            alert = mw.store.add(
                title=title,
                source=source,
                category=category,
                severity=severity,
                affected_entities=entities,
                date=date,
                description=description,
            )
            return f"Alert #{alert.alert_id} added: [{severity.upper()}] {title}. Total alerts: {len(mw.store.alerts)}"

        def list_alerts(
            runtime: ToolRuntime[None, RegulatoryAlertsState],
            category: Annotated[str, "Filter by category (empty for all)"] = "",
            severity: Annotated[str, "Filter by severity (empty for all)"] = "",
            status: Annotated[str, "Filter by status (empty for all)"] = "",
        ) -> str:
            """List all alerts with optional filtering."""
            filtered = mw.store.filter_alerts(category=category, severity=severity, status=status)
            if not filtered:
                return "No alerts matching the specified filters."
            lines = [f"## Regulatory Alerts ({len(filtered)} results)", ""]
            for alert in filtered:
                entities = ", ".join(alert.affected_entities) if alert.affected_entities else "N/A"
                lines.append(f"- **#{alert.alert_id}** [{alert.severity.upper()}] {alert.title}")
                lines.append(f"  Source: {alert.source} | Category: {alert.category} | Status: {alert.status} | Date: {alert.date}")
                lines.append(f"  Affected: {entities}")
                if alert.review_notes:
                    lines.append(f"  Notes: {alert.review_notes}")
                lines.append("")
            return "\n".join(lines)

        def mark_alert_reviewed(
            runtime: ToolRuntime[None, RegulatoryAlertsState],
            alert_id: Annotated[int, "Alert ID to mark as reviewed"],
            notes: Annotated[str, "Review notes"] = "",
            status: Annotated[str, "New status: reviewed or dismissed"] = "reviewed",
        ) -> str:
            """Mark an alert as reviewed with notes."""
            alert = mw.store.get(alert_id)
            if not alert:
                return f"Alert #{alert_id} not found."
            alert.status = status
            alert.review_notes = notes
            return f"Alert #{alert_id} marked as {status}."

        def alert_summary(
            runtime: ToolRuntime[None, RegulatoryAlertsState],
        ) -> str:
            """Generate summary of pending alerts."""
            return mw.store.format_summary()

        def clear_alerts(
            runtime: ToolRuntime[None, RegulatoryAlertsState],
        ) -> str:
            """Clear all alerts."""
            mw.store = AlertStore()
            return "All alerts cleared."

        return [
            StructuredTool.from_function(name="add_alert", description="Add a regulatory alert.", func=add_alert),
            StructuredTool.from_function(
                name="list_alerts", description="List alerts with optional filtering by category, severity, or status.", func=list_alerts
            ),
            StructuredTool.from_function(name="mark_alert_reviewed", description="Mark an alert as reviewed with notes.", func=mark_alert_reviewed),
            StructuredTool.from_function(name="alert_summary", description="Generate summary of pending alerts.", func=alert_summary),
            StructuredTool.from_function(name="clear_alerts", description="Clear all alerts.", func=clear_alerts),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject regulatory alert instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, REGULATORY_SYSTEM_PROMPT))

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


__all__ = ["AlertStore", "RegulatoryAlert", "RegulatoryAlertsMiddleware"]
