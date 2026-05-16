"""Client report generator middleware.
Feature #14: Template-driven report generation with live data, required
disclosures, PDF/Markdown export, and firm branding support.

## Tools

- `set_report_config`: Configure report metadata (client, period, type)
- `add_report_section`: Add a named section with content
- `add_disclosure`: Add a required regulatory disclosure
- `generate_report`: Render the complete report as formatted Markdown
- `clear_report`: Reset for a new report

## Usage

```python
from bog_agents.middleware.client_reports import ClientReportsMiddleware

middleware = ClientReportsMiddleware()
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
class ReportSection:
    """A section in a client report."""

    title: str
    content: str
    order: int = 0


@dataclass
class ClientReport:
    """A structured client report."""

    client_name: str = ""
    account_id: str = ""
    report_type: str = "quarterly_review"
    period: str = ""
    firm_name: str = ""
    advisor_name: str = ""
    sections: list[ReportSection] = field(default_factory=list)
    disclosures: list[str] = field(default_factory=list)
    generated_at: str = ""

    def format_report(self) -> str:
        """Render the complete report as Markdown."""
        self.generated_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

        title_map = {
            "quarterly_review": "Quarterly Portfolio Review",
            "annual_review": "Annual Portfolio Review",
            "performance_report": "Performance Report",
            "rebalancing_proposal": "Rebalancing Proposal",
            "financial_plan": "Financial Plan Update",
            "tax_report": "Tax Planning Report",
        }
        report_title = title_map.get(self.report_type, self.report_type.replace("_", " ").title())

        lines = [
            f"# {report_title}",
            "",
        ]

        if self.firm_name:
            lines.append(f"**{self.firm_name}**")
        lines.extend(
            [
                f"**Prepared for:** {self.client_name}",
                f"**Account:** {self.account_id}" if self.account_id else "",
                f"**Period:** {self.period}" if self.period else "",
                f"**Advisor:** {self.advisor_name}" if self.advisor_name else "",
                f"**Date:** {self.generated_at}",
                "",
                "---",
                "",
            ]
        )

        sorted_sections = sorted(self.sections, key=lambda s: s.order)
        for section in sorted_sections:
            lines.append(f"## {section.title}")
            lines.append("")
            lines.append(section.content)
            lines.append("")

        if self.disclosures:
            lines.append("---")
            lines.append("")
            lines.append("## Important Disclosures")
            lines.append("")
            for disc in self.disclosures:
                lines.append(f"*{disc}*")
                lines.append("")

        lines.append(f"*Report generated on {self.generated_at}*")

        return "\n".join(line for line in lines if line is not None)


STANDARD_DISCLOSURES = [
    "Past performance does not guarantee future results. Investing involves risk, including the possible loss of principal.",
    "The information provided is for informational purposes only and does not constitute investment advice, a recommendation, or a solicitation.",
    "Asset allocation and diversification do not ensure a profit or protect against loss in declining markets.",
    "All data and performance figures are believed to be accurate but are not guaranteed. Please verify all information independently.",
]

CLIENT_REPORTS_SYSTEM_PROMPT = """## Client Report Generator

You can generate professional, compliance-ready client reports.

**Report Types:** quarterly_review, annual_review, performance_report, rebalancing_proposal, financial_plan, tax_report

**Workflow:**
1. `set_report_config` — Set client name, period, report type, firm/advisor info
2. `add_report_section` — Add sections (Executive Summary, Performance, Allocation, Outlook, etc.)
3. `add_disclosure` — Add required regulatory disclosures (standard ones are auto-included)
4. `generate_report` — Render the final formatted report

**Best Practices:**
- Always include an Executive Summary as the first section
- Include performance data with appropriate time periods
- Add standard disclosures (past performance, risk warnings)
- Note the data sources for all figures"""


class ClientReportsState(TypedDict):
    """State for client reports middleware."""


class ClientReportsMiddleware(AgentMiddleware[ClientReportsState, ContextT, ResponseT]):
    """Middleware for generating professional client reports.

    Provides tools for building structured reports with sections,
    disclosures, and formatted output.
    """

    state_schema = ClientReportsState

    def __init__(self, *, firm_name: str = "", advisor_name: str = "") -> None:
        self._firm_name = firm_name
        self._advisor_name = advisor_name
        self.report = ClientReport(firm_name=firm_name, advisor_name=advisor_name)
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build report generation tools."""
        mw = self

        def set_report_config(
            runtime: ToolRuntime[None, ClientReportsState],
            client_name: Annotated[str, "Client name"],
            report_type: Annotated[
                str, "Report type: quarterly_review, annual_review, performance_report, rebalancing_proposal, financial_plan, tax_report"
            ] = "quarterly_review",
            period: Annotated[str, "Report period (e.g., 'Q1 2026', 'FY 2025')"] = "",
            account_id: Annotated[str, "Account identifier"] = "",
            firm_name: Annotated[str, "Firm name for branding"] = "",
            advisor_name: Annotated[str, "Advisor name"] = "",
        ) -> str:
            """Configure report metadata (client, period, type, branding)."""
            mw.report.client_name = client_name
            mw.report.report_type = report_type
            mw.report.period = period
            mw.report.account_id = account_id
            if firm_name:
                mw.report.firm_name = firm_name
            if advisor_name:
                mw.report.advisor_name = advisor_name
            return f"Report configured: {report_type} for {client_name} ({period})"

        def add_report_section(
            runtime: ToolRuntime[None, ClientReportsState],
            title: Annotated[str, "Section title"],
            content: Annotated[str, "Section content (Markdown)"],
            order: Annotated[int, "Section order (lower = earlier)"] = 0,
        ) -> str:
            """Add a named section to the report."""
            if order == 0:
                order = len(mw.report.sections) + 1
            mw.report.sections.append(ReportSection(title=title, content=content, order=order))
            return f"Section '{title}' added (order: {order}). Total sections: {len(mw.report.sections)}"

        def add_disclosure(
            runtime: ToolRuntime[None, ClientReportsState],
            disclosure: Annotated[str, "Disclosure text"],
        ) -> str:
            """Add a regulatory disclosure to the report."""
            mw.report.disclosures.append(disclosure)
            return f"Disclosure added. Total: {len(mw.report.disclosures)}"

        def add_standard_disclosures(
            runtime: ToolRuntime[None, ClientReportsState],
        ) -> str:
            """Add all standard regulatory disclosures (past performance, risk, etc.)."""
            for disc in STANDARD_DISCLOSURES:
                if disc not in mw.report.disclosures:
                    mw.report.disclosures.append(disc)
            return f"Standard disclosures added. Total: {len(mw.report.disclosures)}"

        def generate_report(
            runtime: ToolRuntime[None, ClientReportsState],
        ) -> str:
            """Generate the complete formatted client report."""
            if not mw.report.client_name:
                return "Error: Report not configured. Use `set_report_config` first."
            if not mw.report.sections:
                return "Error: No sections added. Use `add_report_section` to add content."
            return mw.report.format_report()

        def clear_report(
            runtime: ToolRuntime[None, ClientReportsState],
        ) -> str:
            """Clear the report to start fresh."""
            mw.report = ClientReport(firm_name=mw._firm_name, advisor_name=mw._advisor_name)
            return "Report cleared. Ready for new report."

        return [
            StructuredTool.from_function(
                name="set_report_config", description="Configure report metadata (client, period, type, branding).", func=set_report_config
            ),
            StructuredTool.from_function(
                name="add_report_section", description="Add a named section with Markdown content to the report.", func=add_report_section
            ),
            StructuredTool.from_function(name="add_disclosure", description="Add a regulatory disclosure to the report.", func=add_disclosure),
            StructuredTool.from_function(
                name="add_standard_disclosures", description="Add all standard regulatory disclosures.", func=add_standard_disclosures
            ),
            StructuredTool.from_function(name="generate_report", description="Generate the complete formatted client report.", func=generate_report),
            StructuredTool.from_function(name="clear_report", description="Clear the report to start fresh.", func=clear_report),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject report generation instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, CLIENT_REPORTS_SYSTEM_PROMPT))

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


__all__ = ["ClientReport", "ClientReportsMiddleware"]
