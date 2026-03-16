"""Citation-backed research output middleware.

Feature #10: Every claim in agent output includes a citation back to the source
document, filing paragraph, or data point. Supports labeling citations as
supporting, contradicting, or mentioning.

## Overview

The citations middleware provides tools for:

- Registering data sources with metadata
- Adding citations that link claims to sources
- Generating a formatted bibliography
- Validating that all claims have citations

## Citation Format

Citations follow an inline bracket format: `[1]`, `[2]`, etc.
Each citation links to a registered source with:

- Source type (filing, report, article, database, api, manual)
- Confidence level (high, medium, low)
- Relationship to claim (supports, contradicts, mentions)
- Specific excerpt or data point from the source

## Usage

```python
from bog_agents.middleware.citations import CitationsMiddleware

middleware = CitationsMiddleware()
```
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
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


class SourceType(str, Enum):
    """Types of data sources that can be cited."""

    FILING = "filing"
    REPORT = "report"
    ARTICLE = "article"
    DATABASE = "database"
    API = "api"
    MANUAL = "manual"
    WEBSITE = "website"
    TRANSCRIPT = "transcript"


class CitationRelation(str, Enum):
    """Relationship of a citation to the claim it supports."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    MENTIONS = "mentions"


class ConfidenceLevel(str, Enum):
    """Confidence level for a citation."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Source:
    """A registered data source.

    Attributes:
        source_id: Unique identifier for this source.
        title: Human-readable title.
        source_type: Type of source (filing, report, etc.).
        url: URL or path to the source.
        author: Author or organization.
        date: Publication or access date.
        metadata: Additional structured metadata.
    """

    source_id: int
    title: str
    source_type: str = "manual"
    url: str = ""
    author: str = ""
    date: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Citation:
    """A citation linking a claim to a source.

    Attributes:
        citation_id: Unique identifier for this citation.
        source_id: ID of the source being cited.
        claim: The specific claim being supported.
        excerpt: Relevant excerpt from the source.
        relation: How the source relates to the claim.
        confidence: Confidence level of the citation.
        page: Page number or section reference.
        timestamp: When this citation was created.
    """

    citation_id: int
    source_id: int
    claim: str
    excerpt: str = ""
    relation: str = "supports"
    confidence: str = "medium"
    page: str = ""
    timestamp: str = ""


@dataclass
class CitationRegistry:
    """Registry of sources and citations for a research session."""

    sources: list[Source] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    _next_source_id: int = field(default=1, repr=False)
    _next_citation_id: int = field(default=1, repr=False)

    def register_source(
        self,
        *,
        title: str,
        source_type: str = "manual",
        url: str = "",
        author: str = "",
        date: str = "",
        metadata: dict[str, str] | None = None,
    ) -> Source:
        """Register a new data source.

        Args:
            title: Human-readable title.
            source_type: Type of source.
            url: URL or path.
            author: Author or organization.
            date: Publication or access date.
            metadata: Additional metadata.

        Returns:
            The newly registered source.
        """
        source = Source(
            source_id=self._next_source_id,
            title=title,
            source_type=source_type,
            url=url,
            author=author,
            date=date,
            metadata=metadata or {},
        )
        self.sources.append(source)
        self._next_source_id += 1
        return source

    def add_citation(
        self,
        *,
        source_id: int,
        claim: str,
        excerpt: str = "",
        relation: str = "supports",
        confidence: str = "medium",
        page: str = "",
    ) -> Citation | None:
        """Add a citation linking a claim to a source.

        Args:
            source_id: ID of the source being cited.
            claim: The specific claim being supported.
            excerpt: Relevant excerpt from the source.
            relation: Relationship (supports, contradicts, mentions).
            confidence: Confidence level (high, medium, low).
            page: Page number or section reference.

        Returns:
            The newly created citation, or None if source_id is invalid.
        """
        if not any(s.source_id == source_id for s in self.sources):
            logger.warning("Source ID %d not found in registry", source_id)
            return None

        citation = Citation(
            citation_id=self._next_citation_id,
            source_id=source_id,
            claim=claim,
            excerpt=excerpt,
            relation=relation,
            confidence=confidence,
            page=page,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.citations.append(citation)
        self._next_citation_id += 1
        return citation

    def get_source(self, source_id: int) -> Source | None:
        """Get a source by ID.

        Args:
            source_id: The source identifier.

        Returns:
            The source, or None if not found.
        """
        for s in self.sources:
            if s.source_id == source_id:
                return s
        return None

    def format_bibliography(self) -> str:
        """Format a complete bibliography with all sources and citations.

        Returns:
            Formatted bibliography string.
        """
        if not self.sources:
            return "No sources registered. Use `register_source` to add data sources."

        lines = ["## Bibliography", ""]

        for source in self.sources:
            source_citations = [c for c in self.citations if c.source_id == source.source_id]
            citation_count = len(source_citations)

            line = f"**[{source.source_id}]** {source.title}"
            if source.author:
                line += f" — {source.author}"
            if source.date:
                line += f" ({source.date})"
            line += f" [{source.source_type}]"
            if source.url:
                line += f"\n    URL: {source.url}"
            line += f"\n    Cited {citation_count} time(s)"
            lines.append(line)
            lines.append("")

        if self.citations:
            lines.append("## Citation Details")
            lines.append("")
            for cit in self.citations:
                source = self.get_source(cit.source_id)
                source_title = source.title if source else f"Source #{cit.source_id}"
                icon = {"supports": "+", "contradicts": "!", "mentions": "~"}.get(cit.relation, "?")
                lines.append(f"[{icon}] **Citation #{cit.citation_id}** (Source [{cit.source_id}]: {source_title})")
                lines.append(f"    Claim: {cit.claim}")
                if cit.excerpt:
                    lines.append(f'    Excerpt: "{cit.excerpt}"')
                lines.append(f"    Relation: {cit.relation} | Confidence: {cit.confidence}")
                if cit.page:
                    lines.append(f"    Reference: {cit.page}")
                lines.append("")

        return "\n".join(lines)


CITATIONS_SYSTEM_PROMPT = """## Citation Requirements

You MUST cite sources for all factual claims in your research output.

**Citation Format:**
- Use inline bracket citations: [1], [2], etc.
- Register each data source using `register_source` before citing it
- Add citations using `add_citation` to link claims to sources
- Always specify the relationship: supports, contradicts, or mentions
- Set confidence level: high (primary source), medium (secondary), low (unverified)

**When to Cite:**
- Every numerical fact or statistic
- Every claim about a company, person, or regulation
- Every market observation or trend assertion
- Every recommendation or conclusion based on data

**Citation Labels:**
- `supports` — Source directly confirms the claim
- `contradicts` — Source provides evidence against the claim (flag this prominently!)
- `mentions` — Source references the topic but doesn't directly confirm/deny

Use `show_bibliography` to review all registered sources and citations."""


class CitationsState(TypedDict):
    """State for citations middleware."""


class CitationsMiddleware(AgentMiddleware[CitationsState, ContextT, ResponseT]):
    """Middleware for citation-backed research output.

    Provides tools for registering sources, adding citations, and generating
    formatted bibliographies. Injects citation requirements into the system prompt.

    Args:
        require_citations: Whether to enforce citation requirements in prompts.
    """

    state_schema = CitationsState

    def __init__(self, *, require_citations: bool = True) -> None:
        self.registry = CitationRegistry()
        self._require_citations = require_citations
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build citation tools."""
        mw = self

        def register_source(
            runtime: ToolRuntime[None, CitationsState],
            title: Annotated[str, "Title of the data source"],
            source_type: Annotated[str, "Type: filing, report, article, database, api, website, transcript, manual"] = "manual",
            url: Annotated[str, "URL or path to the source"] = "",
            author: Annotated[str, "Author or organization"] = "",
            date: Annotated[str, "Publication or access date"] = "",
        ) -> str:
            """Register a data source for citation. Returns the source ID."""
            source = mw.registry.register_source(
                title=title,
                source_type=source_type,
                url=url,
                author=author,
                date=date,
            )
            return f"Source [{source.source_id}] registered: {title}"

        def add_citation(
            runtime: ToolRuntime[None, CitationsState],
            source_id: Annotated[int, "ID of the registered source"],
            claim: Annotated[str, "The specific claim being cited"],
            excerpt: Annotated[str, "Relevant excerpt from the source"] = "",
            relation: Annotated[str, "Relationship: supports, contradicts, or mentions"] = "supports",
            confidence: Annotated[str, "Confidence: high, medium, or low"] = "medium",
            page: Annotated[str, "Page number or section reference"] = "",
        ) -> str:
            """Add a citation linking a claim to a registered source."""
            citation = mw.registry.add_citation(
                source_id=source_id,
                claim=claim,
                excerpt=excerpt,
                relation=relation,
                confidence=confidence,
                page=page,
            )
            if citation is None:
                return f"Error: Source ID {source_id} not found. Use `register_source` first."
            icon = {"supports": "SUPPORTS", "contradicts": "CONTRADICTS", "mentions": "MENTIONS"}.get(relation, relation)
            return f"Citation #{citation.citation_id} added [{icon}]: {claim}"

        def show_bibliography(
            runtime: ToolRuntime[None, CitationsState],
        ) -> str:
            """Show the complete bibliography with all registered sources and citations."""
            return mw.registry.format_bibliography()

        return [
            StructuredTool.from_function(
                name="register_source",
                description="Register a data source for citation tracking. Call this before citing a source.",
                func=register_source,
            ),
            StructuredTool.from_function(
                name="add_citation",
                description="Add a citation linking a claim to a registered source. Specify relation (supports/contradicts/mentions) and confidence.",
                func=add_citation,
            ),
            StructuredTool.from_function(
                name="show_bibliography",
                description="Show the complete bibliography with all sources and citation details.",
                func=show_bibliography,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject citation requirements into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with citation instructions.
        """
        if not self._require_citations:
            return request

        new_system_message = append_to_system_message(request.system_message, CITATIONS_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject citation requirements into the system prompt.

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
    "Citation",
    "CitationRegistry",
    "CitationRelation",
    "CitationsMiddleware",
    "ConfidenceLevel",
    "Source",
    "SourceType",
]
