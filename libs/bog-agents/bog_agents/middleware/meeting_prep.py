"""Meeting prep agent middleware for financial advisors.
Feature #45: Before a client meeting, automatically prepares a comprehensive
briefing with portfolio performance, material changes, market events, talking
points, and compliance-approved content.

## Overview

The meeting prep middleware provides a structured workflow for preparing
client meetings:

1. **Client Context** — Load client profile, investment policy, preferences
2. **Portfolio Review** — Recent performance, allocation changes, significant trades
3. **Market Context** — Relevant market events since last meeting
4. **Talking Points** — Key discussion items with supporting data
5. **Action Items** — Recommended actions and follow-ups
6. **Compliance Check** — Ensure all materials meet regulatory requirements

## Usage

```python
from bog_agents.middleware.meeting_prep import MeetingPrepMiddleware

middleware = MeetingPrepMiddleware()
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
class ClientProfile:
    """Client profile for meeting preparation.

    Attributes:
        name: Client name.
        account_id: Account identifier.
        risk_tolerance: Risk profile (conservative, moderate, aggressive).
        investment_objectives: Primary investment goals.
        last_meeting_date: Date of most recent meeting.
        notes: Additional notes about the client.
        preferences: Client communication preferences.
    """

    name: str = ""
    account_id: str = ""
    risk_tolerance: str = "moderate"
    investment_objectives: str = ""
    last_meeting_date: str = ""
    notes: str = ""
    preferences: dict[str, str] = field(default_factory=dict)


@dataclass
class TalkingPoint:
    """A talking point for the meeting.

    Attributes:
        topic: Brief topic label.
        content: Detailed content/explanation.
        priority: Priority level (high, medium, low).
        supporting_data: Data points supporting this talking point.
        action_required: Whether client action is needed.
    """

    topic: str
    content: str
    priority: str = "medium"
    supporting_data: list[str] = field(default_factory=list)
    action_required: bool = False


@dataclass
class MeetingBriefing:
    """Complete meeting briefing package.

    Attributes:
        client: Client profile.
        meeting_date: Scheduled meeting date.
        talking_points: Ordered list of discussion items.
        market_summary: Summary of relevant market events.
        portfolio_summary: Portfolio performance and allocation summary.
        action_items: Recommended actions with rationale.
        compliance_notes: Regulatory compliance reminders.
        prepared_at: When this briefing was generated.
    """

    client: ClientProfile = field(default_factory=ClientProfile)
    meeting_date: str = ""
    talking_points: list[TalkingPoint] = field(default_factory=list)
    market_summary: str = ""
    portfolio_summary: str = ""
    action_items: list[str] = field(default_factory=list)
    compliance_notes: list[str] = field(default_factory=list)
    prepared_at: str = ""

    def format_briefing(self) -> str:
        """Format the complete meeting briefing as a readable document.

        Returns:
            Formatted briefing string.
        """
        lines = [
            "# Meeting Briefing",
            f"**Client:** {self.client.name}",
            f"**Account:** {self.client.account_id}",
            f"**Meeting Date:** {self.meeting_date}",
            f"**Risk Profile:** {self.client.risk_tolerance}",
            f"**Prepared:** {self.prepared_at}",
            "",
        ]

        if self.client.investment_objectives:
            lines.extend(["## Investment Objectives", self.client.investment_objectives, ""])

        if self.portfolio_summary:
            lines.extend(["## Portfolio Summary", self.portfolio_summary, ""])

        if self.market_summary:
            lines.extend(["## Market Context", self.market_summary, ""])

        if self.talking_points:
            lines.append("## Talking Points")
            lines.append("")
            for i, tp in enumerate(self.talking_points, 1):
                priority_icon = {"high": "!!!", "medium": "!!", "low": "!"}.get(tp.priority, "!")
                action_flag = " [ACTION REQUIRED]" if tp.action_required else ""
                lines.append(f"### {i}. {tp.topic} ({priority_icon}){action_flag}")
                lines.append(tp.content)
                if tp.supporting_data:
                    lines.append("\n**Supporting Data:**")
                    for data in tp.supporting_data:
                        lines.append(f"- {data}")
                lines.append("")

        if self.action_items:
            lines.append("## Recommended Actions")
            for item in self.action_items:
                lines.append(f"- [ ] {item}")
            lines.append("")

        if self.compliance_notes:
            lines.append("## Compliance Reminders")
            for note in self.compliance_notes:
                lines.append(f"- {note}")
            lines.append("")

        if self.client.notes:
            lines.extend(["## Advisor Notes", self.client.notes, ""])

        return "\n".join(lines)


MEETING_PREP_SYSTEM_PROMPT = """## Meeting Preparation Agent

You are assisting a financial advisor in preparing for a client meeting. Your goal is to produce a comprehensive, compliance-ready meeting briefing.

**Workflow:**
1. Use `set_client_profile` to establish the client context
2. Use `set_portfolio_summary` with the latest portfolio data
3. Use `set_market_summary` with relevant market events
4. Use `add_talking_point` for each discussion item (prioritized)
5. Use `add_action_item` for recommended follow-up actions
6. Use `add_compliance_note` for regulatory reminders
7. Use `generate_briefing` to produce the final formatted document

**Best Practices for Financial Advisor Meetings:**
- Lead with portfolio performance relative to objectives, not benchmarks
- Highlight material changes since the last meeting
- Prepare for likely client questions based on recent market events
- Include specific data points to support every recommendation
- Note any investment policy statement (IPS) considerations
- Include required disclosures and compliance language
- Prepare talking points for both positive and negative scenarios

**Compliance Requirements:**
- All performance data must include appropriate time periods
- Recommendations must be suitable for the client's risk profile
- Note any conflicts of interest
- Include required regulatory disclosures
- Document rationale for every recommendation"""


class MeetingPrepState(TypedDict):
    """State for meeting prep middleware."""


class MeetingPrepMiddleware(AgentMiddleware[MeetingPrepState, ContextT, ResponseT]):
    """Middleware for financial advisor meeting preparation.

    Provides a structured workflow for assembling client meeting briefings
    with portfolio data, market context, talking points, and compliance notes.
    """

    state_schema = MeetingPrepState

    def __init__(self) -> None:
        self.briefing = MeetingBriefing(
            prepared_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build meeting prep tools."""
        mw = self

        def set_client_profile(
            runtime: ToolRuntime[None, MeetingPrepState],
            name: Annotated[str, "Client name"],
            account_id: Annotated[str, "Account identifier"] = "",
            risk_tolerance: Annotated[str, "Risk profile: conservative, moderate, or aggressive"] = "moderate",
            investment_objectives: Annotated[str, "Primary investment goals"] = "",
            last_meeting_date: Annotated[str, "Date of last meeting (YYYY-MM-DD)"] = "",
            notes: Annotated[str, "Additional advisor notes about the client"] = "",
        ) -> str:
            """Set the client profile for the meeting briefing."""
            mw.briefing.client = ClientProfile(
                name=name,
                account_id=account_id,
                risk_tolerance=risk_tolerance,
                investment_objectives=investment_objectives,
                last_meeting_date=last_meeting_date,
                notes=notes,
            )
            return f"Client profile set: {name} ({risk_tolerance} risk, Account: {account_id})"

        def set_meeting_date(
            runtime: ToolRuntime[None, MeetingPrepState],
            date: Annotated[str, "Meeting date (YYYY-MM-DD)"],
        ) -> str:
            """Set the meeting date."""
            mw.briefing.meeting_date = date
            return f"Meeting date set: {date}"

        def set_portfolio_summary(
            runtime: ToolRuntime[None, MeetingPrepState],
            summary: Annotated[str, "Portfolio performance and allocation summary"],
        ) -> str:
            """Set the portfolio summary section of the briefing."""
            mw.briefing.portfolio_summary = summary
            return "Portfolio summary set."

        def set_market_summary(
            runtime: ToolRuntime[None, MeetingPrepState],
            summary: Annotated[str, "Summary of relevant market events since last meeting"],
        ) -> str:
            """Set the market context section of the briefing."""
            mw.briefing.market_summary = summary
            return "Market summary set."

        def add_talking_point(
            runtime: ToolRuntime[None, MeetingPrepState],
            topic: Annotated[str, "Brief topic label"],
            content: Annotated[str, "Detailed content and explanation"],
            priority: Annotated[str, "Priority: high, medium, or low"] = "medium",
            supporting_data: Annotated[str, "Comma-separated supporting data points"] = "",
            action_required: Annotated[bool, "Whether client action is needed"] = False,
        ) -> str:
            """Add a talking point to the meeting briefing, in priority order."""
            data = [d.strip() for d in supporting_data.split(",") if d.strip()] if supporting_data else []
            tp = TalkingPoint(
                topic=topic,
                content=content,
                priority=priority,
                supporting_data=data,
                action_required=action_required,
            )
            mw.briefing.talking_points.append(tp)
            # Sort by priority
            priority_order = {"high": 0, "medium": 1, "low": 2}
            mw.briefing.talking_points.sort(key=lambda t: priority_order.get(t.priority, 1))
            action_flag = " [ACTION REQUIRED]" if action_required else ""
            return f"Talking point added: {topic} ({priority} priority){action_flag}"

        def add_action_item(
            runtime: ToolRuntime[None, MeetingPrepState],
            item: Annotated[str, "Action item description with rationale"],
        ) -> str:
            """Add a recommended action item to the briefing."""
            mw.briefing.action_items.append(item)
            return f"Action item #{len(mw.briefing.action_items)} added."

        def add_compliance_note(
            runtime: ToolRuntime[None, MeetingPrepState],
            note: Annotated[str, "Compliance reminder or required disclosure"],
        ) -> str:
            """Add a compliance note or required disclosure to the briefing."""
            mw.briefing.compliance_notes.append(note)
            return f"Compliance note #{len(mw.briefing.compliance_notes)} added."

        def generate_briefing(
            runtime: ToolRuntime[None, MeetingPrepState],
        ) -> str:
            """Generate the complete formatted meeting briefing document."""
            if not mw.briefing.client.name:
                return "Error: Client profile not set. Use `set_client_profile` first."
            mw.briefing.prepared_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())
            return mw.briefing.format_briefing()

        def reset_briefing(
            runtime: ToolRuntime[None, MeetingPrepState],
        ) -> str:
            """Reset the meeting briefing for a new client/meeting."""
            mw.briefing = MeetingBriefing(
                prepared_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            return "Meeting briefing reset. Ready for new meeting preparation."

        return [
            StructuredTool.from_function(
                name="set_client_profile",
                description="Set the client profile for meeting preparation (name, risk tolerance, objectives, last meeting date).",
                func=set_client_profile,
            ),
            StructuredTool.from_function(
                name="set_meeting_date",
                description="Set the meeting date for the briefing.",
                func=set_meeting_date,
            ),
            StructuredTool.from_function(
                name="set_portfolio_summary",
                description="Set the portfolio performance and allocation summary for the briefing.",
                func=set_portfolio_summary,
            ),
            StructuredTool.from_function(
                name="set_market_summary",
                description="Set the market context summary for the briefing (events since last meeting).",
                func=set_market_summary,
            ),
            StructuredTool.from_function(
                name="add_talking_point",
                description="Add a prioritized talking point with supporting data to the meeting briefing.",
                func=add_talking_point,
            ),
            StructuredTool.from_function(
                name="add_action_item",
                description="Add a recommended action item to the meeting briefing.",
                func=add_action_item,
            ),
            StructuredTool.from_function(
                name="add_compliance_note",
                description="Add a compliance note or required regulatory disclosure to the briefing.",
                func=add_compliance_note,
            ),
            StructuredTool.from_function(
                name="generate_briefing",
                description="Generate the complete formatted meeting briefing document with all sections.",
                func=generate_briefing,
            ),
            StructuredTool.from_function(
                name="reset_briefing",
                description="Reset the meeting briefing to start fresh for a new client or meeting.",
                func=reset_briefing,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject meeting prep instructions into system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        new_system_message = append_to_system_message(request.system_message, MEETING_PREP_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject meeting prep instructions.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = [
    "ClientProfile",
    "MeetingBriefing",
    "MeetingPrepMiddleware",
    "TalkingPoint",
]
