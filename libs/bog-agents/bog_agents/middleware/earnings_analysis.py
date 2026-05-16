"""Earnings call live analysis middleware.
Feature #37: Real-time transcript analysis for earnings calls with
speaker tracking, metric extraction, guidance recording, and
comprehensive summary generation.

## Tools

- `start_earnings_session`: Start a new earnings analysis session
- `add_transcript_segment`: Add a transcript segment
- `add_metric_mention`: Record a financial metric mentioned
- `add_guidance_item`: Record forward guidance
- `earnings_summary`: Generate comprehensive earnings analysis
- `clear_earnings`: Clear session

## Usage

```python
from bog_agents.middleware.earnings_analysis import EarningsAnalysisMiddleware

middleware = EarningsAnalysisMiddleware()
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
class TranscriptSegment:
    """A segment of an earnings call transcript.

    Attributes:
        speaker: Name of the speaker.
        role: Role of the speaker (ceo, cfo, analyst, etc.).
        text: Transcript text.
        timestamp: When this segment was added.
    """

    speaker: str
    role: str
    text: str
    timestamp: str = ""


@dataclass
class MetricMention:
    """A financial metric mentioned during the call.

    Attributes:
        metric_name: Name of the metric (e.g., revenue, EPS).
        value: Reported value.
        period: Time period (e.g., Q3 2025, FY2025).
        context: Additional context around the mention.
    """

    metric_name: str
    value: str
    period: str
    context: str = ""


@dataclass
class GuidanceItem:
    """A forward guidance item from management.

    Attributes:
        metric: Metric being guided on.
        guidance_value: The guidance value or range.
        period: Period the guidance applies to.
        direction: Direction of change (raised, lowered, maintained, new).
    """

    metric: str
    guidance_value: str
    period: str
    direction: str


@dataclass
class EarningsSession:
    """An earnings call analysis session.

    Attributes:
        company: Company name or ticker.
        quarter: Quarter (e.g., Q3).
        year: Fiscal year.
        segments: Transcript segments.
        metrics: Metrics mentioned during the call.
        guidance: Forward guidance items.
        started_at: Session start timestamp.
    """

    company: str
    quarter: str
    year: int
    segments: list[TranscriptSegment] = field(default_factory=list)
    metrics: list[MetricMention] = field(default_factory=list)
    guidance: list[GuidanceItem] = field(default_factory=list)
    started_at: str = ""

    def format_summary(self) -> str:
        """Format comprehensive earnings analysis summary.

        Returns:
            Markdown-formatted earnings summary.
        """
        lines = [
            "## Earnings Call Analysis",
            f"**Company:** {self.company}",
            f"**Period:** {self.quarter} {self.year}",
            f"**Session Started:** {self.started_at}",
            f"**Transcript Segments:** {len(self.segments)}",
            "",
        ]

        # Overview
        lines.append("### Overview")
        lines.append("")
        role_counts: dict[str, int] = {}
        for seg in self.segments:
            role_counts[seg.role] = role_counts.get(seg.role, 0) + 1
        if role_counts:
            parts = [f"{role}: {count}" for role, count in sorted(role_counts.items())]
            lines.append(f"Segments by role: {', '.join(parts)}")
        else:
            lines.append("No transcript segments recorded.")
        lines.append("")

        # Key Metrics Discussed
        lines.append("### Key Metrics Discussed")
        lines.append("")
        if self.metrics:
            for m in self.metrics:
                lines.append(f"- **{m.metric_name}**: {m.value} ({m.period})")
                if m.context:
                    lines.append(f"  Context: {m.context}")
        else:
            lines.append("No metrics recorded.")
        lines.append("")

        # Forward Guidance
        lines.append("### Forward Guidance")
        lines.append("")
        if self.guidance:
            direction_icons = {
                "raised": "^",
                "lowered": "v",
                "maintained": "=",
                "new": "+",
            }
            for g in self.guidance:
                icon = direction_icons.get(g.direction, "?")
                lines.append(f"- [{icon}] **{g.metric}**: {g.guidance_value} ({g.period}) — {g.direction}")
        else:
            lines.append("No guidance items recorded.")
        lines.append("")

        # Management Commentary
        lines.append("### Management Commentary")
        lines.append("")

        ceo_segments = [s for s in self.segments if s.role == "ceo"]
        cfo_segments = [s for s in self.segments if s.role == "cfo"]
        analyst_segments = [s for s in self.segments if s.role == "analyst"]
        other_segments = [s for s in self.segments if s.role not in ("ceo", "cfo", "analyst")]

        if ceo_segments:
            lines.append("**CEO Commentary:**")
            for s in ceo_segments:
                lines.append(f"- {s.speaker}: {s.text}")
            lines.append("")

        if cfo_segments:
            lines.append("**CFO Commentary:**")
            for s in cfo_segments:
                lines.append(f"- {s.speaker}: {s.text}")
            lines.append("")

        if analyst_segments:
            lines.append("**Analyst Questions/Comments:**")
            for s in analyst_segments:
                lines.append(f"- {s.speaker}: {s.text}")
            lines.append("")

        if other_segments:
            lines.append("**Other Participants:**")
            for s in other_segments:
                lines.append(f"- {s.speaker} ({s.role}): {s.text}")
            lines.append("")

        if not self.segments:
            lines.append("No commentary recorded.")
            lines.append("")

        return "\n".join(lines)


EARNINGS_SYSTEM_PROMPT = """## Earnings Call Live Analysis

You have tools to analyze earnings call transcripts in real time.

**Speaker Roles:** ceo, cfo, analyst, investor_relations, other
**Guidance Directions:** raised, lowered, maintained, new

**Workflow:**
1. `start_earnings_session` — Begin a new session for a company/quarter
2. `add_transcript_segment` — Add transcript segments as they occur
3. `add_metric_mention` — Record specific financial metrics mentioned
4. `add_guidance_item` — Record forward guidance from management
5. `earnings_summary` — Generate comprehensive analysis
6. `clear_earnings` — Reset for a new session

Track key metrics, management tone, guidance changes, and analyst sentiment."""


class EarningsAnalysisState(TypedDict):
    """State for earnings analysis middleware."""


class EarningsAnalysisMiddleware(AgentMiddleware[EarningsAnalysisState, ContextT, ResponseT]):
    """Middleware for real-time earnings call transcript analysis."""

    state_schema = EarningsAnalysisState

    def __init__(self) -> None:
        self.session: EarningsSession | None = None
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build earnings analysis tools."""
        mw = self

        def start_earnings_session(
            runtime: ToolRuntime[None, EarningsAnalysisState],
            company: Annotated[str, "Company name or ticker"],
            quarter: Annotated[str, "Quarter (e.g., Q1, Q2, Q3, Q4)"],
            year: Annotated[int, "Fiscal year"],
        ) -> str:
            """Start a new earnings analysis session."""
            mw.session = EarningsSession(
                company=company,
                quarter=quarter,
                year=year,
                started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            return f"Earnings session started: {company} {quarter} {year}. Ready to receive transcript segments, metrics, and guidance."

        def add_transcript_segment(
            runtime: ToolRuntime[None, EarningsAnalysisState],
            speaker: Annotated[str, "Name of the speaker"],
            role: Annotated[str, "Role: ceo, cfo, analyst, investor_relations, other"],
            text: Annotated[str, "Transcript text"],
        ) -> str:
            """Add a transcript segment from the earnings call."""
            if mw.session is None:
                return "Error: No active session. Use `start_earnings_session` first."
            segment = TranscriptSegment(
                speaker=speaker,
                role=role,
                text=text,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.session.segments.append(segment)
            return f"Segment added: {speaker} ({role}). Total segments: {len(mw.session.segments)}"

        def add_metric_mention(
            runtime: ToolRuntime[None, EarningsAnalysisState],
            metric_name: Annotated[str, "Name of the metric (e.g., revenue, EPS, margins)"],
            value: Annotated[str, "Reported value"],
            period: Annotated[str, "Time period (e.g., Q3 2025, FY2025)"],
            context: Annotated[str, "Additional context around the mention"] = "",
        ) -> str:
            """Record a financial metric mentioned during the call."""
            if mw.session is None:
                return "Error: No active session. Use `start_earnings_session` first."
            metric = MetricMention(
                metric_name=metric_name,
                value=value,
                period=period,
                context=context,
            )
            mw.session.metrics.append(metric)
            return f"Metric recorded: {metric_name} = {value} ({period}). Total metrics: {len(mw.session.metrics)}"

        def add_guidance_item(
            runtime: ToolRuntime[None, EarningsAnalysisState],
            metric: Annotated[str, "Metric being guided on"],
            guidance_value: Annotated[str, "Guidance value or range"],
            period: Annotated[str, "Period the guidance applies to"],
            direction: Annotated[str, "Direction: raised, lowered, maintained, new"],
        ) -> str:
            """Record forward guidance from management."""
            if mw.session is None:
                return "Error: No active session. Use `start_earnings_session` first."
            item = GuidanceItem(
                metric=metric,
                guidance_value=guidance_value,
                period=period,
                direction=direction,
            )
            mw.session.guidance.append(item)
            return f"Guidance recorded: {metric} {direction} to {guidance_value} for {period}. Total guidance items: {len(mw.session.guidance)}"

        def earnings_summary(
            runtime: ToolRuntime[None, EarningsAnalysisState],
        ) -> str:
            """Generate comprehensive earnings analysis summary."""
            if mw.session is None:
                return "Error: No active session. Use `start_earnings_session` first."
            return mw.session.format_summary()

        def clear_earnings(
            runtime: ToolRuntime[None, EarningsAnalysisState],
        ) -> str:
            """Clear the current earnings session."""
            mw.session = None
            return "Earnings session cleared. Ready for a new session."

        return [
            StructuredTool.from_function(
                name="start_earnings_session",
                description="Start a new earnings analysis session for a company and quarter.",
                func=start_earnings_session,
            ),
            StructuredTool.from_function(
                name="add_transcript_segment", description="Add a transcript segment from the earnings call.", func=add_transcript_segment
            ),
            StructuredTool.from_function(
                name="add_metric_mention", description="Record a financial metric mentioned during the call.", func=add_metric_mention
            ),
            StructuredTool.from_function(name="add_guidance_item", description="Record forward guidance from management.", func=add_guidance_item),
            StructuredTool.from_function(
                name="earnings_summary", description="Generate comprehensive earnings analysis summary.", func=earnings_summary
            ),
            StructuredTool.from_function(name="clear_earnings", description="Clear the current earnings session.", func=clear_earnings),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject earnings analysis instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, EARNINGS_SYSTEM_PROMPT))

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


__all__ = ["EarningsAnalysisMiddleware", "EarningsSession", "GuidanceItem", "MetricMention", "TranscriptSegment"]
