"""Hallucination detection middleware.

Feature #31: Cross-validates numerical claims against source data, flags
unsourced assertions, and provides confidence scoring. Critical for financial
advice where wrong numbers create liability.

## Overview

The hallucination detection middleware provides tools for:

- Registering verified facts from primary sources
- Checking claims against the verified fact database
- Flagging unsourced or contradicted assertions
- Generating a verification report with trust scores

## How It Works

1. As the agent gathers data, it registers verified facts with `register_fact`
2. Before including claims in output, it checks them with `verify_claim`
3. The middleware tracks verification status of all claims
4. `verification_report` shows which claims are verified, unverified, or contradicted

## Usage

```python
from bog_agents.middleware.hallucination_detection import HallucinationDetectionMiddleware

middleware = HallucinationDetectionMiddleware()
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
class VerifiedFact:
    """A fact verified from a primary source.

    Attributes:
        fact_id: Unique identifier.
        content: The factual statement.
        source: Where this fact came from.
        category: Category (financial, regulatory, market, company, general).
        verified_at: When this fact was verified.
        numerical_value: If the fact contains a number, store it for comparison.
        unit: Unit of measurement if applicable.
    """

    fact_id: int
    content: str
    source: str
    category: str = "general"
    verified_at: str = ""
    numerical_value: float | None = None
    unit: str = ""


@dataclass
class ClaimCheck:
    """Result of checking a claim against verified facts.

    Attributes:
        claim_id: Unique identifier.
        claim: The claim being checked.
        status: Verification status (verified, unverified, contradicted, partial).
        matching_facts: IDs of facts that match this claim.
        contradicting_facts: IDs of facts that contradict this claim.
        confidence: Confidence score (0.0 to 1.0).
        notes: Explanation of the verification result.
        checked_at: When this check was performed.
    """

    claim_id: int
    claim: str
    status: str = "unverified"
    matching_facts: list[int] = field(default_factory=list)
    contradicting_facts: list[int] = field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""
    checked_at: str = ""


@dataclass
class FactDatabase:
    """Database of verified facts and claim checks."""

    facts: list[VerifiedFact] = field(default_factory=list)
    claims: list[ClaimCheck] = field(default_factory=list)
    _next_fact_id: int = field(default=1, repr=False)
    _next_claim_id: int = field(default=1, repr=False)

    def register_fact(
        self,
        *,
        content: str,
        source: str,
        category: str = "general",
        numerical_value: float | None = None,
        unit: str = "",
    ) -> VerifiedFact:
        """Register a verified fact from a primary source.

        Args:
            content: The factual statement.
            source: Where this fact came from.
            category: Fact category.
            numerical_value: Numerical value if applicable.
            unit: Unit of measurement.

        Returns:
            The registered fact.
        """
        fact = VerifiedFact(
            fact_id=self._next_fact_id,
            content=content,
            source=source,
            category=category,
            verified_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            numerical_value=numerical_value,
            unit=unit,
        )
        self.facts.append(fact)
        self._next_fact_id += 1
        return fact

    def check_claim(
        self,
        *,
        claim: str,
        matching_fact_ids: list[int] | None = None,
        contradicting_fact_ids: list[int] | None = None,
        confidence: float = 0.0,
        notes: str = "",
    ) -> ClaimCheck:
        """Check a claim against the verified fact database.

        Args:
            claim: The claim to verify.
            matching_fact_ids: IDs of facts supporting this claim.
            contradicting_fact_ids: IDs of facts contradicting this claim.
            confidence: Confidence score.
            notes: Explanation.

        Returns:
            The claim check result.
        """
        matching = matching_fact_ids or []
        contradicting = contradicting_fact_ids or []

        if contradicting:
            status = "contradicted"
        elif matching:
            status = "verified"
        elif confidence > 0.5:  # noqa: PLR2004
            status = "partial"
        else:
            status = "unverified"

        check = ClaimCheck(
            claim_id=self._next_claim_id,
            claim=claim,
            status=status,
            matching_facts=matching,
            contradicting_facts=contradicting,
            confidence=confidence,
            notes=notes,
            checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.claims.append(check)
        self._next_claim_id += 1
        return check

    @property
    def verification_stats(self) -> dict[str, int]:
        """Get counts of claims by verification status."""
        stats: dict[str, int] = {"verified": 0, "unverified": 0, "contradicted": 0, "partial": 0}
        for claim in self.claims:
            stats[claim.status] = stats.get(claim.status, 0) + 1
        return stats

    @property
    def trust_score(self) -> float:
        """Calculate overall trust score for the session.

        Returns:
            Trust score from 0.0 (all claims unverified) to 1.0 (all verified).
        """
        if not self.claims:
            return 0.0
        verified = sum(1 for c in self.claims if c.status == "verified")
        return verified / len(self.claims)

    def format_report(self) -> str:
        """Format a verification report.

        Returns:
            Formatted verification report string.
        """
        if not self.facts and not self.claims:
            return "No facts registered and no claims checked yet."

        stats = self.verification_stats
        lines = [
            "## Hallucination Detection Report",
            f"Verified Facts: {len(self.facts)}",
            f"Claims Checked: {len(self.claims)}",
            f"Trust Score: {self.trust_score:.0%}",
            "",
            "### Claim Status Breakdown",
            f"  Verified:     {stats.get('verified', 0)}",
            f"  Unverified:   {stats.get('unverified', 0)}",
            f"  Contradicted: {stats.get('contradicted', 0)}",
            f"  Partial:      {stats.get('partial', 0)}",
            "",
        ]

        # Show contradicted claims first (most important)
        contradicted = [c for c in self.claims if c.status == "contradicted"]
        if contradicted:
            lines.append("### CONTRADICTED CLAIMS (requires immediate attention)")
            for c in contradicted:
                lines.append(f"  ! Claim #{c.claim_id}: {c.claim}")
                lines.append(f"    Contradicted by facts: {', '.join(f'#{f}' for f in c.contradicting_facts)}")
                if c.notes:
                    lines.append(f"    Notes: {c.notes}")
                lines.append("")

        # Show unverified claims
        unverified = [c for c in self.claims if c.status == "unverified"]
        if unverified:
            lines.append("### UNVERIFIED CLAIMS (needs source)")
            for c in unverified:
                lines.append(f"  ? Claim #{c.claim_id}: {c.claim}")
                if c.notes:
                    lines.append(f"    Notes: {c.notes}")
                lines.append("")

        # Show verified claims
        verified = [c for c in self.claims if c.status == "verified"]
        if verified:
            lines.append("### Verified Claims")
            for c in verified:
                lines.append(f"  + Claim #{c.claim_id}: {c.claim} (confidence: {c.confidence:.0%})")
            lines.append("")

        return "\n".join(lines)


HALLUCINATION_SYSTEM_PROMPT = """## Hallucination Prevention

You MUST verify factual claims before including them in research output.

**Verification Workflow:**
1. When you find data from a source, register it with `register_fact`
2. Before stating a fact in your response, check it with `verify_claim`
3. NEVER include unverified numerical claims in financial analysis
4. If a claim is contradicted, flag it prominently and explain the discrepancy
5. Use `verification_report` to review the trust score before finalizing output

**Verification Levels:**
- Verified (confidence >= 0.8): Claim matches a registered fact from a primary source
- Partial (confidence 0.5-0.8): Claim partially supported but not fully confirmed
- Unverified (confidence < 0.5): No supporting facts found — MUST be flagged
- Contradicted: A registered fact directly contradicts this claim — MUST be highlighted

**Financial Accuracy Rules:**
- NEVER guess financial figures — always verify against source data
- If you can't verify a number, say "unverified" explicitly
- Round numbers appropriately and note the source
- Flag any discrepancies between sources"""


class HallucinationDetectionState(TypedDict):
    """State for hallucination detection middleware."""


class HallucinationDetectionMiddleware(AgentMiddleware[HallucinationDetectionState, ContextT, ResponseT]):
    """Middleware for hallucination detection and fact verification.

    Provides tools for registering verified facts, checking claims, and
    generating verification reports with trust scores.
    """

    state_schema = HallucinationDetectionState

    def __init__(self) -> None:
        self.db = FactDatabase()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build hallucination detection tools."""
        mw = self

        def register_fact(
            runtime: ToolRuntime[None, HallucinationDetectionState],
            content: Annotated[str, "The factual statement"],
            source: Annotated[str, "Where this fact came from"],
            category: Annotated[str, "Category: financial, regulatory, market, company, general"] = "general",
            numerical_value: Annotated[float | None, "Numerical value if applicable"] = None,
            unit: Annotated[str, "Unit of measurement if applicable"] = "",
        ) -> str:
            """Register a verified fact from a primary source for later verification checks."""
            fact = mw.db.register_fact(
                content=content,
                source=source,
                category=category,
                numerical_value=numerical_value,
                unit=unit,
            )
            return f"Fact #{fact.fact_id} registered: {content[:80]}"

        def verify_claim(
            runtime: ToolRuntime[None, HallucinationDetectionState],
            claim: Annotated[str, "The claim to verify"],
            matching_fact_ids: Annotated[str, "Comma-separated IDs of facts supporting this claim"] = "",
            contradicting_fact_ids: Annotated[str, "Comma-separated IDs of facts contradicting this claim"] = "",
            confidence: Annotated[float, "Confidence score from 0.0 to 1.0"] = 0.0,
            notes: Annotated[str, "Explanation of the verification result"] = "",
        ) -> str:
            """Check a claim against verified facts. Returns verification status."""
            matching = [int(i.strip()) for i in matching_fact_ids.split(",") if i.strip()] if matching_fact_ids else []
            contradicting = [int(i.strip()) for i in contradicting_fact_ids.split(",") if i.strip()] if contradicting_fact_ids else []
            check = mw.db.check_claim(
                claim=claim,
                matching_fact_ids=matching,
                contradicting_fact_ids=contradicting,
                confidence=confidence,
                notes=notes,
            )
            icon = {"verified": "+", "unverified": "?", "contradicted": "!", "partial": "~"}.get(check.status, "?")
            return f"[{icon}] Claim #{check.claim_id} [{check.status.upper()}]: {claim[:80]}"

        def verification_report(
            runtime: ToolRuntime[None, HallucinationDetectionState],
        ) -> str:
            """Show the full verification report with trust score and claim status breakdown."""
            return mw.db.format_report()

        return [
            StructuredTool.from_function(
                name="register_fact",
                description="Register a verified fact from a primary source. Use this as you gather data to build the verification database.",
                func=register_fact,
            ),
            StructuredTool.from_function(
                name="verify_claim",
                description="Check a claim against verified facts. Links to matching/contradicting facts and returns verification status.",
                func=verify_claim,
            ),
            StructuredTool.from_function(
                name="verification_report",
                description="Show the full hallucination detection report with trust score, claim status breakdown, and flagged issues.",
                func=verification_report,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject hallucination prevention instructions into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        new_system_message = append_to_system_message(request.system_message, HALLUCINATION_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject hallucination prevention instructions.

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


__all__ = ["ClaimCheck", "FactDatabase", "HallucinationDetectionMiddleware", "VerifiedFact"]
