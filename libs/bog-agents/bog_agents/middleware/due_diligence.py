"""Smart Due Diligence Workflow middleware for guided investment evaluation.

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
class DDChecklistItem:
    """A single checklist item in a due diligence workflow."""

    item_id: int
    category: str  # financial, legal, operational, market, regulatory, management
    description: str
    status: str = "pending"  # pending, pass, fail, flag, na
    findings: str = ""
    evidence_sources: list[str] = field(default_factory=list)
    reviewed_by: str = ""
    reviewed_at: str = ""


@dataclass
class DDWorkflow:
    """A due diligence workflow for evaluating an investment."""

    workflow_id: int
    company: str
    deal_type: str  # equity, debt, ma, partnership
    checklist: list[DDChecklistItem] = field(default_factory=list)
    overall_status: str = "pending"
    started_at: str = ""
    _next_item_id: int = 1


@dataclass
class DDStore:
    """Storage for due diligence workflows."""

    workflows: dict[int, DDWorkflow] = field(default_factory=dict)
    active_workflow_id: int | None = None
    _next_workflow_id: int = 1


SYSTEM_PROMPT = """You have access to due diligence workflow tools for guided investment evaluation. \
Categories: financial, legal, operational, market, regulatory, management. Deal types: equity, debt, \
ma, partnership. Item statuses: pending, pass, fail, flag, na. Use these tools to create workflows, \
manage checklists, update findings, and generate reports."""


class DueDiligenceState(TypedDict):
    """State for due diligence middleware."""


class DueDiligenceMiddleware(AgentMiddleware[DueDiligenceState, ContextT, ResponseT]):
    """Middleware providing guided due diligence workflows for investment evaluation."""

    state_schema = DueDiligenceState

    def __init__(self) -> None:
        self.store = DDStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build the due diligence tools."""
        mw = self

        def start_due_diligence(
            runtime: ToolRuntime[None, DueDiligenceState],
            company: Annotated[str, "Name of the company being evaluated"],
            deal_type: Annotated[str, "Deal type: equity, debt, ma, or partnership"],
        ) -> str:
            """Start a new due diligence workflow for a company."""
            if deal_type not in ("equity", "debt", "ma", "partnership"):
                return f"Invalid deal type: {deal_type}. Must be equity, debt, ma, or partnership."
            wf = DDWorkflow(
                workflow_id=mw.store._next_workflow_id,
                company=company,
                deal_type=deal_type,
                started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.store.workflows[mw.store._next_workflow_id] = wf
            mw.store.active_workflow_id = mw.store._next_workflow_id
            mw.store._next_workflow_id += 1
            logger.info("Started DD workflow %d for %s (%s)", wf.workflow_id, company, deal_type)
            return f"Due diligence workflow #{wf.workflow_id} started for '{company}' (deal type: {deal_type})."

        def add_checklist_item(
            runtime: ToolRuntime[None, DueDiligenceState],
            workflow_id: Annotated[int, "ID of the due diligence workflow"],
            category: Annotated[str, "Category: financial, legal, operational, market, regulatory, or management"],
            description: Annotated[str, "Description of the checklist item"],
        ) -> str:
            """Add a checklist item to a due diligence workflow."""
            wf = mw.store.workflows.get(workflow_id)
            if not wf:
                return f"Workflow #{workflow_id} not found."
            if category not in ("financial", "legal", "operational", "market", "regulatory", "management"):
                return f"Invalid category: {category}. Must be financial, legal, operational, market, regulatory, or management."
            item = DDChecklistItem(
                item_id=wf._next_item_id,
                category=category,
                description=description,
            )
            wf._next_item_id += 1
            wf.checklist.append(item)
            logger.info("Added DD item %d to workflow %d: %s", item.item_id, workflow_id, category)
            return f"Checklist item #{item.item_id} added to workflow #{workflow_id}: [{category}] {description}"

        def update_item_status(
            runtime: ToolRuntime[None, DueDiligenceState],
            workflow_id: Annotated[int, "ID of the due diligence workflow"],
            item_id: Annotated[int, "ID of the checklist item"],
            status: Annotated[str, "New status: pending, pass, fail, flag, or na"],
            findings: Annotated[str, "Findings or notes for this item"] = "",
            evidence_sources: Annotated[str, "Comma-separated evidence sources"] = "",
            reviewed_by: Annotated[str, "Name of the reviewer"] = "",
        ) -> str:
            """Update the status and findings of a checklist item."""
            wf = mw.store.workflows.get(workflow_id)
            if not wf:
                return f"Workflow #{workflow_id} not found."
            if status not in ("pending", "pass", "fail", "flag", "na"):
                return f"Invalid status: {status}. Must be pending, pass, fail, flag, or na."
            item = None
            for i in wf.checklist:
                if i.item_id == item_id:
                    item = i
                    break
            if not item:
                return f"Item #{item_id} not found in workflow #{workflow_id}."
            item.status = status
            if findings:
                item.findings = findings
            if evidence_sources:
                item.evidence_sources = [s.strip() for s in evidence_sources.split(",")]
            if reviewed_by:
                item.reviewed_by = reviewed_by
            item.reviewed_at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())
            logger.info("Updated DD item %d in workflow %d to %s", item_id, workflow_id, status)
            return f"Item #{item_id} in workflow #{workflow_id} updated to '{status}'."

        def dd_report(
            runtime: ToolRuntime[None, DueDiligenceState],
            workflow_id: Annotated[int, "ID of the due diligence workflow"],
        ) -> str:
            """Generate a due diligence report for a workflow."""
            wf = mw.store.workflows.get(workflow_id)
            if not wf:
                return f"Workflow #{workflow_id} not found."
            lines = [
                f"# Due Diligence Report: {wf.company}",
                f"Deal Type: {wf.deal_type} | Started: {wf.started_at}",
                f"Overall Status: {wf.overall_status}",
                "",
            ]
            categories = ["financial", "legal", "operational", "market", "regulatory", "management"]
            for cat in categories:
                items = [i for i in wf.checklist if i.category == cat]
                if not items:
                    continue
                lines.append(f"## {cat.title()}")
                for item in items:
                    icon = {"pending": "[ ]", "pass": "[+]", "fail": "[-]", "flag": "[!]", "na": "[~]"}.get(item.status, "[ ]")
                    lines.append(f"  {icon} #{item.item_id}: {item.description} ({item.status})")
                    if item.findings:
                        lines.append(f"      Findings: {item.findings}")
                    if item.evidence_sources:
                        lines.append(f"      Sources: {', '.join(item.evidence_sources)}")
                    if item.reviewed_by:
                        lines.append(f"      Reviewed by: {item.reviewed_by} at {item.reviewed_at}")
                lines.append("")
            total = len(wf.checklist)
            if total:
                passed = sum(1 for i in wf.checklist if i.status == "pass")
                failed = sum(1 for i in wf.checklist if i.status == "fail")
                flagged = sum(1 for i in wf.checklist if i.status == "flag")
                pending = sum(1 for i in wf.checklist if i.status == "pending")
                lines.append(f"Summary: {total} items — {passed} pass, {failed} fail, {flagged} flagged, {pending} pending")
            else:
                lines.append("No checklist items yet.")
            return "\n".join(lines)

        def clear_due_diligence(
            runtime: ToolRuntime[None, DueDiligenceState],
        ) -> str:
            """Clear all due diligence workflows."""
            count = len(mw.store.workflows)
            mw.store.workflows.clear()
            mw.store.active_workflow_id = None
            mw.store._next_workflow_id = 1
            logger.info("Cleared %d DD workflows", count)
            return f"Cleared {count} due diligence workflow(s)."

        return [
            StructuredTool.from_function(
                func=start_due_diligence,
                name="start_due_diligence",
                description="Start a new due diligence workflow for a company.",
            ),
            StructuredTool.from_function(
                func=add_checklist_item,
                name="add_checklist_item",
                description="Add a checklist item to a due diligence workflow.",
            ),
            StructuredTool.from_function(
                func=update_item_status,
                name="update_item_status",
                description="Update the status and findings of a checklist item.",
            ),
            StructuredTool.from_function(
                func=dd_report,
                name="dd_report",
                description="Generate a due diligence report for a workflow.",
            ),
            StructuredTool.from_function(
                func=clear_due_diligence,
                name="clear_due_diligence",
                description="Clear all due diligence workflows.",
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append due diligence system prompt to the request."""
        return request.override(
            system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT),
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Wrap synchronous model call with due diligence context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Wrap asynchronous model call with due diligence context."""
        return await call_next(self.modify_request(request))


__all__ = [
    "DDChecklistItem",
    "DDStore",
    "DDWorkflow",
    "DueDiligenceMiddleware",
]
