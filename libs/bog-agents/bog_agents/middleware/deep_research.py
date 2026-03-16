"""Multi-source deep research agent middleware.

Feature #19: Takes a research question, searches across ALL connected sources
simultaneously, cross-references findings, and produces a structured report
with citations, confidence scores, and contradictions noted.

## Tools

- `start_research`: Begin a new research task with a question
- `add_finding`: Record a finding from a specific source
- `add_contradiction`: Record contradictions between findings
- `research_summary`: Generate the structured research report
- `research_status`: Check current research progress

## Usage

```python
from bog_agents.middleware.deep_research import DeepResearchMiddleware

middleware = DeepResearchMiddleware()
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


@dataclass
class Finding:
    """A research finding from a specific source."""

    finding_id: int
    content: str
    source: str
    source_type: str = "general"
    confidence: float = 0.8
    timestamp: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Contradiction:
    """A contradiction between two findings."""

    finding_a_id: int
    finding_b_id: int
    description: str
    resolution: str = ""


@dataclass
class ResearchTask:
    """A deep research task with findings and analysis."""

    question: str = ""
    findings: list[Finding] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    conclusion: str = ""
    started_at: str = ""
    _next_id: int = field(default=1, repr=False)

    def add_finding(
        self,
        *,
        content: str,
        source: str,
        source_type: str = "general",
        confidence: float = 0.8,
        tags: list[str] | None = None,
    ) -> Finding:
        """Record a finding.

        Args:
            content: The finding content.
            source: Source of the finding.
            source_type: Type (web, filing, database, report, api, manual).
            confidence: Confidence level (0.0 to 1.0).
            tags: Classification tags.

        Returns:
            The recorded finding.
        """
        finding = Finding(
            finding_id=self._next_id,
            content=content,
            source=source,
            source_type=source_type,
            confidence=confidence,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            tags=tags or [],
        )
        self.findings.append(finding)
        self._next_id += 1
        return finding

    def add_contradiction(
        self,
        *,
        finding_a_id: int,
        finding_b_id: int,
        description: str,
        resolution: str = "",
    ) -> Contradiction | None:
        """Record a contradiction between findings.

        Args:
            finding_a_id: First finding ID.
            finding_b_id: Second finding ID.
            description: Description of the contradiction.
            resolution: How the contradiction was resolved.

        Returns:
            The recorded contradiction, or None if IDs are invalid.
        """
        ids = {f.finding_id for f in self.findings}
        if finding_a_id not in ids or finding_b_id not in ids:
            return None
        c = Contradiction(
            finding_a_id=finding_a_id,
            finding_b_id=finding_b_id,
            description=description,
            resolution=resolution,
        )
        self.contradictions.append(c)
        return c

    @property
    def source_count(self) -> int:
        """Number of unique sources consulted."""
        return len({f.source for f in self.findings})

    @property
    def avg_confidence(self) -> float:
        """Average confidence across all findings."""
        if not self.findings:
            return 0.0
        return sum(f.confidence for f in self.findings) / len(self.findings)

    def format_report(self) -> str:
        """Generate the structured research report."""
        lines = [
            "# Deep Research Report",
            f"**Question:** {self.question}",
            f"**Started:** {self.started_at}",
            f"**Findings:** {len(self.findings)} from {self.source_count} sources",
            f"**Average Confidence:** {self.avg_confidence:.0%}",
            f"**Contradictions Found:** {len(self.contradictions)}",
            "",
        ]

        if self.findings:
            lines.append("## Findings")
            lines.append("")
            by_source: dict[str, list[Finding]] = {}
            for f in self.findings:
                by_source.setdefault(f.source, []).append(f)

            for source, findings in by_source.items():
                lines.append(f"### Source: {source}")
                for f in findings:
                    tags = f" [{', '.join(f.tags)}]" if f.tags else ""
                    lines.append(f"- **[{f.finding_id}]** (confidence: {f.confidence:.0%}){tags} {f.content}")
                lines.append("")

        if self.contradictions:
            lines.append("## Contradictions")
            lines.append("")
            for c in self.contradictions:
                lines.append(f"- Finding #{c.finding_a_id} vs #{c.finding_b_id}: {c.description}")
                if c.resolution:
                    lines.append(f"  **Resolution:** {c.resolution}")
            lines.append("")

        if self.conclusion:
            lines.append("## Conclusion")
            lines.append(self.conclusion)

        return "\n".join(lines)


DEEP_RESEARCH_SYSTEM_PROMPT = """## Deep Research Mode

You are conducting deep, multi-source research. Follow this workflow:

1. **Start**: Use `start_research` with the research question
2. **Gather**: Search ALL available sources and record findings with `add_finding`
3. **Cross-reference**: Identify contradictions with `add_contradiction`
4. **Synthesize**: Set your conclusion and generate the report with `research_summary`

**Source Types:** web, filing, database, report, api, manual

**Key Principles:**
- Search broadly — consult as many sources as possible
- Record EVERY finding, even minor ones
- Flag ALL contradictions between sources
- Assign honest confidence scores
- Higher confidence for primary sources (filings, databases)
- Lower confidence for secondary sources (news, commentary)"""


class DeepResearchState(TypedDict):
    """State for deep research middleware."""


class DeepResearchMiddleware(AgentMiddleware[DeepResearchState, ContextT, ResponseT]):
    """Middleware for multi-source deep research.

    Provides a structured workflow for conducting research across multiple
    sources, recording findings, identifying contradictions, and generating
    synthesis reports.
    """

    state_schema = DeepResearchState

    def __init__(self) -> None:
        self.task = ResearchTask()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build deep research tools."""
        mw = self

        def start_research(
            runtime: ToolRuntime[None, DeepResearchState],
            question: Annotated[str, "The research question to investigate"],
        ) -> str:
            """Start a new deep research task. Clears any previous research."""
            mw.task = ResearchTask(
                question=question,
                started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            return f"Research started: {question}"

        def add_finding(
            runtime: ToolRuntime[None, DeepResearchState],
            content: Annotated[str, "The finding content"],
            source: Annotated[str, "Source name or URL"],
            source_type: Annotated[str, "Type: web, filing, database, report, api, manual"] = "general",
            confidence: Annotated[float, "Confidence (0.0 to 1.0)"] = 0.8,
            tags: Annotated[str, "Comma-separated tags"] = "",
        ) -> str:
            """Record a research finding from a specific source."""
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            f = mw.task.add_finding(
                content=content,
                source=source,
                source_type=source_type,
                confidence=confidence,
                tags=tag_list,
            )
            return f"Finding #{f.finding_id} recorded from {source} (confidence: {confidence:.0%})"

        def add_contradiction(
            runtime: ToolRuntime[None, DeepResearchState],
            finding_a_id: Annotated[int, "First finding ID"],
            finding_b_id: Annotated[int, "Second finding ID"],
            description: Annotated[str, "Description of the contradiction"],
            resolution: Annotated[str, "How the contradiction was resolved"] = "",
        ) -> str:
            """Record a contradiction between two findings."""
            c = mw.task.add_contradiction(
                finding_a_id=finding_a_id,
                finding_b_id=finding_b_id,
                description=description,
                resolution=resolution,
            )
            if c is None:
                return "Error: Invalid finding IDs."
            return f"Contradiction recorded: #{finding_a_id} vs #{finding_b_id}"

        def set_research_conclusion(
            runtime: ToolRuntime[None, DeepResearchState],
            conclusion: Annotated[str, "The synthesized conclusion"],
        ) -> str:
            """Set the final research conclusion."""
            mw.task.conclusion = conclusion
            return "Research conclusion set."

        def research_summary(
            runtime: ToolRuntime[None, DeepResearchState],
        ) -> str:
            """Generate the complete deep research report."""
            if not mw.task.question:
                return "No research in progress. Use `start_research` first."
            return mw.task.format_report()

        def research_status(
            runtime: ToolRuntime[None, DeepResearchState],
        ) -> str:
            """Check current research progress."""
            return (
                f"Question: {mw.task.question or '(not started)'}\n"
                f"Findings: {len(mw.task.findings)} from {mw.task.source_count} sources\n"
                f"Contradictions: {len(mw.task.contradictions)}\n"
                f"Avg Confidence: {mw.task.avg_confidence:.0%}\n"
                f"Conclusion: {'Set' if mw.task.conclusion else 'Not yet'}"
            )

        return [
            StructuredTool.from_function(name="start_research", description="Start a new deep research task.", func=start_research),
            StructuredTool.from_function(name="add_finding", description="Record a finding from a specific source.", func=add_finding),
            StructuredTool.from_function(
                name="add_contradiction", description="Record a contradiction between two findings.", func=add_contradiction
            ),
            StructuredTool.from_function(
                name="set_research_conclusion", description="Set the final research conclusion.", func=set_research_conclusion
            ),
            StructuredTool.from_function(name="research_summary", description="Generate the complete deep research report.", func=research_summary),
            StructuredTool.from_function(name="research_status", description="Check current research progress.", func=research_status),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject deep research instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, DEEP_RESEARCH_SYSTEM_PROMPT))

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


__all__ = ["DeepResearchMiddleware", "Finding", "ResearchTask"]
