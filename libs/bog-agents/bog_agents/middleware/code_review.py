"""Code review agent middleware.

Feature #7: Second-opinion validation for research outputs. Submit content
for review, add comments, generate checklists, and produce pass/fail summaries.

## Tools

- `submit_for_review`: Submit research text for review
- `add_review_comment`: Add a review comment on a specific section
- `review_checklist`: Generate a review checklist for the content
- `review_summary`: Generate overall review summary with pass/fail
- `clear_review`: Clear review state

## Usage

```python
from bog_agents.middleware.code_review import CodeReviewMiddleware

middleware = CodeReviewMiddleware()
```
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
class ReviewComment:
    """A single review comment.

    Attributes:
        comment_id: Unique identifier.
        section: Section of the content being commented on.
        content: The comment text.
        severity: Severity level (info, warning, error).
        reviewer: Name or role of the reviewer.
    """

    comment_id: int
    section: str
    content: str
    severity: str = "info"
    reviewer: str = "agent"


@dataclass
class ReviewSession:
    """A review session with submitted content and comments.

    Attributes:
        submitted_content: The text submitted for review.
        comments: List of review comments.
        checklist: List of checklist items.
        overall_status: Review status (pending, approved, needs_revision, rejected).
    """

    submitted_content: str = ""
    comments: list[ReviewComment] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    overall_status: str = "pending"
    _next_id: int = field(default=1, repr=False)

    def add_comment(
        self,
        section: str,
        content: str,
        severity: str = "info",
        reviewer: str = "agent",
    ) -> ReviewComment:
        """Add a review comment.

        Args:
            section: Section being commented on.
            content: Comment text.
            severity: Severity level.
            reviewer: Reviewer identity.

        Returns:
            The created comment.
        """
        comment = ReviewComment(
            comment_id=self._next_id,
            section=section,
            content=content,
            severity=severity,
            reviewer=reviewer,
        )
        self.comments.append(comment)
        self._next_id += 1
        return comment

    def generate_checklist(self) -> list[str]:
        """Generate a standard review checklist.

        Returns:
            List of checklist items.
        """
        self.checklist = [
            "Sources cited?",
            "Data current?",
            "Methodology sound?",
            "Conclusions supported?",
            "Risks disclosed?",
            "Compliance checked?",
        ]
        return self.checklist

    def format_summary(self) -> str:
        """Format a markdown review summary.

        Returns:
            Markdown-formatted review summary.
        """
        if not self.submitted_content:
            return "No content submitted for review."

        error_count = sum(1 for c in self.comments if c.severity == "error")
        warning_count = sum(1 for c in self.comments if c.severity == "warning")
        info_count = sum(1 for c in self.comments if c.severity == "info")

        lines = [
            "## Review Summary",
            f"Status: **{self.overall_status}**",
            f"Comments: {len(self.comments)} ({error_count} errors, {warning_count} warnings, {info_count} info)",
            "",
        ]

        if self.comments:
            lines.append("### Comments")
            for c in self.comments:
                icon = {"error": "X", "warning": "!", "info": "i"}.get(c.severity, "?")
                lines.append(f"- [{icon}] **{c.section}** ({c.severity}): {c.content} — _{c.reviewer}_")
            lines.append("")

        if self.checklist:
            lines.append("### Checklist")
            for item in self.checklist:
                lines.append(f"- [ ] {item}")
            lines.append("")

        preview = self.submitted_content[:200]
        if len(self.submitted_content) > 200:
            preview += "..."
        lines.append("### Content Preview")
        lines.append(preview)

        return "\n".join(lines)


CODE_REVIEW_SYSTEM_PROMPT = """## Code Review Tools

You have access to research review tools for second-opinion validation.

**Workflow:**
1. `submit_for_review` — Submit the research content
2. `add_review_comment` — Add comments on specific sections (info/warning/error)
3. `review_checklist` — Generate a standard review checklist
4. `review_summary` — Produce a pass/fail summary

**Severity Levels:**
- `info`: Observation or suggestion
- `warning`: Potential issue that should be addressed
- `error`: Critical issue that must be fixed before approval

**Statuses:** pending, approved, needs_revision, rejected"""


class CodeReviewState(TypedDict):
    """State for code review middleware."""


class CodeReviewMiddleware(AgentMiddleware[CodeReviewState, ContextT, ResponseT]):
    """Middleware for second-opinion validation of research outputs."""

    state_schema = CodeReviewState

    def __init__(self) -> None:
        self.session = ReviewSession()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build code review tools."""
        mw = self

        def submit_for_review(
            runtime: ToolRuntime[None, CodeReviewState],
            content: Annotated[str, "Research text to submit for review"],
        ) -> str:
            """Submit research text for review."""
            mw.session = ReviewSession(submitted_content=content)
            return f"Content submitted for review ({len(content)} characters). Status: pending."

        def add_review_comment(
            runtime: ToolRuntime[None, CodeReviewState],
            section: Annotated[str, "Section of the content being commented on"],
            content: Annotated[str, "The review comment text"],
            severity: Annotated[str, "Severity: info, warning, or error"] = "info",
            reviewer: Annotated[str, "Reviewer name or role"] = "agent",
        ) -> str:
            """Add a review comment on a specific section."""
            if not mw.session.submitted_content:
                return "No content submitted. Use `submit_for_review` first."
            comment = mw.session.add_comment(
                section=section,
                content=content,
                severity=severity,
                reviewer=reviewer,
            )
            return f"Comment #{comment.comment_id} added ({severity}) on '{section}'. Total comments: {len(mw.session.comments)}"

        def review_checklist(
            runtime: ToolRuntime[None, CodeReviewState],
        ) -> str:
            """Generate a review checklist for the content."""
            if not mw.session.submitted_content:
                return "No content submitted. Use `submit_for_review` first."
            items = mw.session.generate_checklist()
            lines = ["## Review Checklist", ""]
            for item in items:
                lines.append(f"- [ ] {item}")
            return "\n".join(lines)

        def review_summary(
            runtime: ToolRuntime[None, CodeReviewState],
            status: Annotated[str, "Overall status: pending, approved, needs_revision, rejected"] = "pending",
        ) -> str:
            """Generate overall review summary with pass/fail."""
            mw.session.overall_status = status
            return mw.session.format_summary()

        def clear_review(
            runtime: ToolRuntime[None, CodeReviewState],
        ) -> str:
            """Clear review state."""
            mw.session = ReviewSession()
            return "Review session cleared."

        return [
            StructuredTool.from_function(name="submit_for_review", description="Submit research text for review.", func=submit_for_review),
            StructuredTool.from_function(
                name="add_review_comment", description="Add a review comment on a specific section.", func=add_review_comment
            ),
            StructuredTool.from_function(name="review_checklist", description="Generate a review checklist for the content.", func=review_checklist),
            StructuredTool.from_function(name="review_summary", description="Generate overall review summary with pass/fail.", func=review_summary),
            StructuredTool.from_function(name="clear_review", description="Clear review state.", func=clear_review),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject code review instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, CODE_REVIEW_SYSTEM_PROMPT))

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


__all__ = ["CodeReviewMiddleware", "ReviewComment", "ReviewSession"]
