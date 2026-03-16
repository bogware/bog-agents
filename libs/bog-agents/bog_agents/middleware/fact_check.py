"""Fact-check agent middleware for independent claim verification.

Feature #32: Independent verification of every claim with evidence tracking,
verdicts, and comprehensive reporting.

## Overview

The fact-check middleware provides tools for:

- Submitting claims for fact-checking
- Adding supporting or contradicting evidence
- Setting verdicts with explanations
- Generating comprehensive fact-check reports

## Claim Categories

Supported categories: financial, legal, statistical, general.

## Verdicts

Possible verdicts: verified, false, unverifiable, partially_true.

## Usage

```python
from bog_agents.middleware.fact_check import FactCheckMiddleware

middleware = FactCheckMiddleware()
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
class Evidence:
    """A piece of evidence for or against a claim.

    Attributes:
        text: The evidence text.
        source: Where the evidence came from.
        supports: True if the evidence supports the claim, False if it contradicts.
    """

    text: str
    source: str
    supports: bool


@dataclass
class Claim:
    """A claim submitted for fact-checking.

    Attributes:
        claim_id: Unique identifier for this claim.
        text: The claim text.
        source: Where the claim originated.
        category: Category (financial, legal, statistical, general).
        evidence: List of evidence items.
        verdict: Current verdict (pending, verified, false, unverifiable, partially_true).
        explanation: Explanation of the verdict.
        submitted_at: ISO 8601 timestamp when the claim was submitted.
    """

    claim_id: int
    text: str
    source: str
    category: str
    evidence: list[Evidence] = field(default_factory=list)
    verdict: str = "pending"
    explanation: str = ""
    submitted_at: str = ""


@dataclass
class FactCheckStore:
    """Store managing claims and their fact-check status.

    Attributes:
        claims: List of all submitted claims.
    """

    claims: list[Claim] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def submit(
        self,
        *,
        text: str,
        source: str,
        category: str,
    ) -> Claim:
        """Submit a new claim for fact-checking.

        Args:
            text: The claim text.
            source: Where the claim originated.
            category: Claim category.

        Returns:
            The newly submitted claim.
        """
        claim = Claim(
            claim_id=self._next_id,
            text=text,
            source=source,
            category=category,
            submitted_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.claims.append(claim)
        self._next_id += 1
        logger.debug("Submitted claim #%d: %s", claim.claim_id, text[:80])
        return claim

    def get(self, claim_id: int) -> Claim | None:
        """Get a claim by ID.

        Args:
            claim_id: The claim identifier.

        Returns:
            The claim, or None if not found.
        """
        for claim in self.claims:
            if claim.claim_id == claim_id:
                return claim
        return None

    def add_evidence(
        self,
        *,
        claim_id: int,
        text: str,
        source: str,
        supports: bool,
    ) -> Evidence | None:
        """Add evidence to an existing claim.

        Args:
            claim_id: ID of the claim.
            text: The evidence text.
            source: Where the evidence came from.
            supports: Whether the evidence supports the claim.

        Returns:
            The newly added evidence, or None if the claim was not found.
        """
        claim = self.get(claim_id)
        if claim is None:
            logger.warning("Claim #%d not found", claim_id)
            return None
        evidence = Evidence(text=text, source=source, supports=supports)
        claim.evidence.append(evidence)
        logger.debug(
            "Added %s evidence for claim #%d",
            "supporting" if supports else "contradicting",
            claim_id,
        )
        return evidence

    def set_verdict(
        self,
        *,
        claim_id: int,
        verdict: str,
        explanation: str,
    ) -> Claim | None:
        """Set the verdict for a claim.

        Args:
            claim_id: ID of the claim.
            verdict: The verdict (verified, false, unverifiable, partially_true).
            explanation: Explanation of the verdict.

        Returns:
            The updated claim, or None if not found.
        """
        claim = self.get(claim_id)
        if claim is None:
            logger.warning("Claim #%d not found", claim_id)
            return None
        claim.verdict = verdict
        claim.explanation = explanation
        logger.debug("Verdict for claim #%d: %s", claim_id, verdict)
        return claim

    def format_report(self) -> str:
        """Format a comprehensive fact-check report.

        Returns:
            Formatted fact-check report string with stats and verdict breakdown.
        """
        if not self.claims:
            return "No claims submitted. Use `submit_claim` to begin fact-checking."

        total = len(self.claims)
        verdict_counts: dict[str, int] = {}
        for claim in self.claims:
            verdict_counts[claim.verdict] = verdict_counts.get(claim.verdict, 0) + 1

        verified = verdict_counts.get("verified", 0)
        false = verdict_counts.get("false", 0)
        unverifiable = verdict_counts.get("unverifiable", 0)
        partially_true = verdict_counts.get("partially_true", 0)
        pending = verdict_counts.get("pending", 0)

        lines = [
            "## Fact-Check Report",
            f"Total claims: {total}",
            f"Verified: {verified} | False: {false} | Partially true: {partially_true} | Unverifiable: {unverifiable} | Pending: {pending}",
            "",
            "### Verdict Breakdown",
        ]

        for verdict_type, count in sorted(verdict_counts.items()):
            lines.append(f"- {verdict_type}: {count}")
        lines.append("")

        # Detail each claim
        lines.append("### Claims")
        for claim in self.claims:
            icon = {
                "verified": "V",
                "false": "X",
                "unverifiable": "?",
                "partially_true": "~",
                "pending": "-",
            }.get(claim.verdict, "-")
            lines.append(f"#### [{icon}] Claim #{claim.claim_id} ({claim.verdict})")
            lines.append(f"**Text:** {claim.text}")
            lines.append(f"**Source:** {claim.source}")
            lines.append(f"**Category:** {claim.category}")
            lines.append(f"**Submitted:** {claim.submitted_at}")
            if claim.explanation:
                lines.append(f"**Explanation:** {claim.explanation}")

            if claim.evidence:
                lines.append(f"**Evidence ({len(claim.evidence)}):**")
                for ev in claim.evidence:
                    stance = "SUPPORTS" if ev.supports else "CONTRADICTS"
                    lines.append(f"  - [{stance}] {ev.text} (source: {ev.source})")
            else:
                lines.append("**Evidence:** None submitted")
            lines.append("")

        return "\n".join(lines)


FACT_CHECK_SYSTEM_PROMPT = """## Fact-Check Agent

You have access to a fact-checking system for independent verification of claims.

**Workflow:**
1. Use `submit_claim` to register claims for fact-checking
2. Use `add_evidence` to attach supporting or contradicting evidence
3. Use `verdict` to set the final verdict with explanation
4. Use `fact_check_report` to review all claims and their status

**Verdict Options:**
- `verified` — Claim confirmed by evidence
- `false` — Claim contradicted by evidence
- `partially_true` — Claim partially supported, with caveats
- `unverifiable` — Insufficient evidence to determine truth

**Categories:** financial, legal, statistical, general

Always provide evidence before setting a verdict. Flag contradicting evidence prominently."""


class FactCheckState(TypedDict):
    """State for fact-check middleware."""


class FactCheckMiddleware(AgentMiddleware[FactCheckState, ContextT, ResponseT]):
    """Middleware for independent fact-checking of claims.

    Provides tools for submitting claims, adding evidence, setting verdicts,
    and generating comprehensive fact-check reports.
    """

    state_schema = FactCheckState

    def __init__(self) -> None:
        self.store = FactCheckStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build fact-check tools."""
        mw = self

        def submit_claim(
            runtime: ToolRuntime[None, FactCheckState],
            claim: Annotated[str, "The claim text to fact-check"],
            source: Annotated[str, "Where the claim originated"],
            category: Annotated[str, "Category: financial, legal, statistical, or general"] = "general",
        ) -> str:
            """Submit a claim for fact-checking."""
            c = mw.store.submit(text=claim, source=source, category=category)
            return f"Claim #{c.claim_id} submitted [{category}]: {claim}"

        def add_evidence(
            runtime: ToolRuntime[None, FactCheckState],
            claim_id: Annotated[int, "ID of the claim to add evidence for"],
            evidence_text: Annotated[str, "The evidence text"],
            evidence_source: Annotated[str, "Where the evidence came from"],
            supports: Annotated[bool, "True if evidence supports the claim, False if it contradicts"] = True,
        ) -> str:
            """Add supporting or contradicting evidence for a claim."""
            ev = mw.store.add_evidence(
                claim_id=claim_id,
                text=evidence_text,
                source=evidence_source,
                supports=supports,
            )
            if ev is None:
                return f"Error: Claim #{claim_id} not found."
            stance = "supporting" if supports else "contradicting"
            return f"Added {stance} evidence for claim #{claim_id}."

        def verdict(
            runtime: ToolRuntime[None, FactCheckState],
            claim_id: Annotated[int, "ID of the claim to set verdict for"],
            verdict: Annotated[str, "Verdict: verified, false, unverifiable, or partially_true"],
            explanation: Annotated[str, "Explanation of the verdict"],
        ) -> str:
            """Set the verdict for a claim."""
            c = mw.store.set_verdict(
                claim_id=claim_id,
                verdict=verdict,
                explanation=explanation,
            )
            if c is None:
                return f"Error: Claim #{claim_id} not found."
            return f"Verdict for claim #{claim_id}: {verdict}. {explanation}"

        def fact_check_report(
            runtime: ToolRuntime[None, FactCheckState],
        ) -> str:
            """Generate the full fact-check report with stats and verdict breakdown."""
            return mw.store.format_report()

        def clear_fact_checks(
            runtime: ToolRuntime[None, FactCheckState],
        ) -> str:
            """Clear all claims and reset the fact-check store."""
            count = len(mw.store.claims)
            mw.store.claims.clear()
            mw.store._next_id = 1
            return f"Cleared {count} claims. Fact-check store reset."

        return [
            StructuredTool.from_function(
                name="submit_claim",
                description="Submit a claim for fact-checking with a source and category.",
                func=submit_claim,
            ),
            StructuredTool.from_function(
                name="add_evidence",
                description="Add supporting or contradicting evidence for a submitted claim.",
                func=add_evidence,
            ),
            StructuredTool.from_function(
                name="verdict",
                description="Set the verdict for a claim (verified, false, unverifiable, partially_true) with explanation.",
                func=verdict,
            ),
            StructuredTool.from_function(
                name="fact_check_report",
                description="Generate the full fact-check report with stats and verdict breakdown.",
                func=fact_check_report,
            ),
            StructuredTool.from_function(
                name="clear_fact_checks",
                description="Clear all claims and reset the fact-check store.",
                func=clear_fact_checks,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject fact-check instructions into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with fact-check instructions.
        """
        new_system_message = append_to_system_message(request.system_message, FACT_CHECK_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject fact-check instructions.

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


__all__ = ["Claim", "Evidence", "FactCheckMiddleware", "FactCheckStore"]
