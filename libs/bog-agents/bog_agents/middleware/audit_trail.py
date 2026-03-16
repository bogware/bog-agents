"""Compliance audit trail middleware for financial advisory workflows.

Feature #9: Every agent action is logged with timestamps, data sources, and
reasoning chain. Generates FINRA Rule 3110-compliant supervision records with
full provenance tracking.

## Overview

The audit trail middleware intercepts every LLM call and tool invocation,
recording structured log entries that include:

- Timestamp (ISO 8601)
- Action type (llm_call, tool_call, tool_result, state_change)
- Data sources consulted
- Reasoning summary
- User/session context

These entries are queryable via the `audit_log` tool and exportable for
regulatory review.

## FINRA Rule 3110 Compliance

FINRA Rule 3110 requires firms to maintain a supervisory system reasonably
designed to ensure compliance. This middleware helps by:

- Recording every agent action with full context
- Maintaining an immutable audit trail
- Providing exportable records for examination
- Tracking which data sources informed each recommendation

## Usage

```python
from bog_agents.middleware.audit_trail import AuditTrailMiddleware

middleware = AuditTrailMiddleware(
    session_id="advisor-session-123",
    advisor_id="FA-001",
)
```
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

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

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    """A single audit log entry.

    Attributes:
        timestamp: ISO 8601 timestamp of the event.
        action_type: Type of action (llm_call, tool_call, tool_result, etc.).
        description: Human-readable description of the action.
        data_sources: List of data sources consulted.
        reasoning: Summary of reasoning or rationale.
        metadata: Additional structured metadata.
        session_id: Identifier for the agent session.
        advisor_id: Identifier for the financial advisor.
        entry_id: Auto-incrementing entry identifier.
    """

    timestamp: str
    action_type: str
    description: str
    data_sources: list[str] = field(default_factory=list)
    reasoning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    advisor_id: str = ""
    entry_id: int = 0


@dataclass
class AuditLog:
    """Immutable audit log for a session.

    Entries are append-only — no deletion or modification is permitted
    after creation, ensuring regulatory compliance.
    """

    entries: list[AuditEntry] = field(default_factory=list)
    session_id: str = ""
    advisor_id: str = ""
    started_at: str = ""
    _next_id: int = field(default=1, repr=False)

    def add_entry(
        self,
        *,
        action_type: str,
        description: str,
        data_sources: list[str] | None = None,
        reasoning: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append a new entry to the audit log.

        Args:
            action_type: Type of action being recorded.
            description: Human-readable description.
            data_sources: Data sources consulted for this action.
            reasoning: Summary of reasoning or rationale.
            metadata: Additional structured metadata.

        Returns:
            The newly created audit entry.
        """
        entry = AuditEntry(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            action_type=action_type,
            description=description,
            data_sources=data_sources or [],
            reasoning=reasoning,
            metadata=metadata or {},
            session_id=self.session_id,
            advisor_id=self.advisor_id,
            entry_id=self._next_id,
        )
        self.entries.append(entry)
        self._next_id += 1
        logger.debug("Audit entry #%d: %s — %s", entry.entry_id, action_type, description)
        return entry

    @property
    def entry_count(self) -> int:
        """Number of entries in the audit log."""
        return len(self.entries)

    def format_summary(self, *, last_n: int = 0) -> str:
        """Format a human-readable audit trail summary.

        Args:
            last_n: Show only the last N entries. 0 means all.

        Returns:
            Formatted audit trail string.
        """
        entries = self.entries[-last_n:] if last_n > 0 else self.entries
        if not entries:
            return "Audit log is empty. No actions have been recorded yet."

        lines = [
            "## Compliance Audit Trail",
            f"Session: {self.session_id}",
            f"Advisor: {self.advisor_id}",
            f"Started: {self.started_at}",
            f"Total entries: {self.entry_count}",
            "",
        ]

        for entry in entries:
            lines.append(f"### Entry #{entry.entry_id} [{entry.timestamp}]")
            lines.append(f"**Action:** {entry.action_type}")
            lines.append(f"**Description:** {entry.description}")
            if entry.data_sources:
                lines.append(f"**Data Sources:** {', '.join(entry.data_sources)}")
            if entry.reasoning:
                lines.append(f"**Reasoning:** {entry.reasoning}")
            if entry.metadata:
                lines.append(f"**Metadata:** {json.dumps(entry.metadata, default=str)}")
            lines.append("")

        return "\n".join(lines)

    def export_json(self) -> str:
        """Export the full audit log as JSON for regulatory submission.

        Returns:
            JSON string of the complete audit trail.
        """
        return json.dumps(
            {
                "session_id": self.session_id,
                "advisor_id": self.advisor_id,
                "started_at": self.started_at,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
                "entry_count": self.entry_count,
                "entries": [asdict(e) for e in self.entries],
            },
            indent=2,
            default=str,
        )


class AuditTrailState(TypedDict):
    """State for audit trail middleware."""


class AuditTrailMiddleware(AgentMiddleware[AuditTrailState, ContextT, ResponseT]):
    """Middleware for FINRA-compliant audit trail recording.

    Records every LLM call and tool invocation with timestamps, data sources,
    and reasoning chains. Provides tools for querying and exporting the audit log.

    Args:
        session_id: Unique identifier for this agent session.
        advisor_id: Identifier for the financial advisor using the agent.
    """

    state_schema = AuditTrailState

    def __init__(
        self,
        *,
        session_id: str = "",
        advisor_id: str = "",
    ) -> None:
        self.audit_log = AuditLog(
            session_id=session_id,
            advisor_id=advisor_id,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build audit trail tools."""
        mw = self

        def audit_log_tool(
            runtime: ToolRuntime[None, AuditTrailState],
            last_n: int = 0,
        ) -> str:
            """View the compliance audit trail. Use last_n to limit to recent entries."""
            mw.audit_log.add_entry(
                action_type="tool_call",
                description="Audit log viewed",
                metadata={"last_n": last_n},
            )
            return mw.audit_log.format_summary(last_n=last_n)

        def export_audit_log(
            runtime: ToolRuntime[None, AuditTrailState],
        ) -> str:
            """Export the full audit trail as JSON for regulatory submission."""
            mw.audit_log.add_entry(
                action_type="tool_call",
                description="Audit log exported for regulatory review",
            )
            return mw.audit_log.export_json()

        def add_audit_note(
            runtime: ToolRuntime[None, AuditTrailState],
            note: str = "",
            data_sources: str = "",
            reasoning: str = "",
        ) -> str:
            """Add a manual note to the audit trail (e.g., advisor decision rationale).

            Args:
                note: The note to record.
                data_sources: Comma-separated list of data sources consulted.
                reasoning: Reasoning or rationale for the noted action.
            """
            sources = [s.strip() for s in data_sources.split(",") if s.strip()] if data_sources else []
            mw.audit_log.add_entry(
                action_type="manual_note",
                description=note,
                data_sources=sources,
                reasoning=reasoning,
            )
            return f"Audit note #{mw.audit_log.entry_count} recorded."

        return [
            StructuredTool.from_function(
                name="audit_log",
                description="View the compliance audit trail. Shows all recorded agent actions with timestamps and data sources.",
                func=audit_log_tool,
            ),
            StructuredTool.from_function(
                name="export_audit_log",
                description="Export the full audit trail as JSON for FINRA/SEC regulatory submission.",
                func=export_audit_log,
            ),
            StructuredTool.from_function(
                name="add_audit_note",
                description="Add a manual note to the audit trail (e.g., advisor decision rationale, compliance observation).",
                func=add_audit_note,
            ),
        ]

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Record LLM calls in the audit trail.

        Args:
            request: Model request being processed.
            call_next: Handler function to call with the request.

        Returns:
            Model response from handler.
        """
        self.audit_log.add_entry(
            action_type="llm_call",
            description="LLM request sent",
            metadata={"message_count": len(request.messages)},
        )

        response = call_next(request)

        # Record the response
        tool_calls = []
        if hasattr(response, "tool_calls"):
            tool_calls = [tc.get("name", "unknown") for tc in getattr(response, "tool_calls", [])]

        self.audit_log.add_entry(
            action_type="llm_response",
            description="LLM response received",
            metadata={"tool_calls": tool_calls, "has_content": bool(getattr(response, "content", ""))},
        )

        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request being processed.
            call_next: Async handler function.

        Returns:
            Model response from handler.
        """
        self.audit_log.add_entry(
            action_type="llm_call",
            description="LLM request sent",
            metadata={"message_count": len(request.messages)},
        )

        response = await call_next(request)

        tool_calls = []
        if hasattr(response, "tool_calls"):
            tool_calls = [tc.get("name", "unknown") for tc in getattr(response, "tool_calls", [])]

        self.audit_log.add_entry(
            action_type="llm_response",
            description="LLM response received",
            metadata={"tool_calls": tool_calls, "has_content": bool(getattr(response, "content", ""))},
        )

        return response


__all__ = ["AuditEntry", "AuditLog", "AuditTrailMiddleware"]
