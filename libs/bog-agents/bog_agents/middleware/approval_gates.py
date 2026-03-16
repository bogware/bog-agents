"""Human-in-the-loop approval gates middleware.

Feature #35: Multi-level approval workflows with configurable gates,
submission tracking, and approval/rejection recording.

## Tools

- `create_approval_gate`: Create an approval gate
- `submit_for_approval`: Submit an action for approval at a gate
- `record_approval`: Record an approval or rejection
- `gate_status`: Check status of all gates and pending submissions
- `clear_gates`: Clear all gates

## Usage

```python
from bog_agents.middleware.approval_gates import ApprovalGatesMiddleware

middleware = ApprovalGatesMiddleware()
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
class ApprovalGate:
    """An approval gate definition.

    Attributes:
        name: Gate name.
        required_approvers: Number of approvals required.
        description: Description of what this gate controls.
    """

    name: str
    required_approvers: int
    description: str


@dataclass
class ApprovalSubmission:
    """A submission awaiting approval.

    Attributes:
        submission_id: Unique identifier.
        gate_name: Name of the gate this was submitted to.
        action_description: Description of the action requiring approval.
        risk_level: Risk level (low, medium, high, critical).
        status: Current status (pending, approved, rejected).
        approvals: List of (approver, decision, notes) tuples.
        submitted_at: Timestamp when submitted.
    """

    submission_id: int
    gate_name: str
    action_description: str
    risk_level: str
    status: str = "pending"
    approvals: list[tuple[str, str, str]] = field(default_factory=list)
    submitted_at: str = ""


@dataclass
class ApprovalStore:
    """Store for approval gates and submissions.

    Attributes:
        gates: Map of gate name to gate definition.
        submissions: List of all submissions.
    """

    gates: dict[str, ApprovalGate] = field(default_factory=dict)
    submissions: list[ApprovalSubmission] = field(default_factory=list)
    _next_id: int = field(default=1, repr=False)

    def create_gate(
        self,
        name: str,
        required_approvers: int,
        description: str,
    ) -> ApprovalGate:
        """Create an approval gate.

        Args:
            name: Gate name.
            required_approvers: Number of approvals required.
            description: Gate description.

        Returns:
            The created gate.
        """
        gate = ApprovalGate(
            name=name,
            required_approvers=required_approvers,
            description=description,
        )
        self.gates[name] = gate
        return gate

    def submit(
        self,
        gate_name: str,
        action_description: str,
        risk_level: str,
    ) -> ApprovalSubmission:
        """Submit an action for approval.

        Args:
            gate_name: Name of the gate to submit to.
            action_description: Description of the action.
            risk_level: Risk level (low, medium, high, critical).

        Returns:
            The created submission.
        """
        submission = ApprovalSubmission(
            submission_id=self._next_id,
            gate_name=gate_name,
            action_description=action_description,
            risk_level=risk_level,
            submitted_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        )
        self.submissions.append(submission)
        self._next_id += 1
        return submission

    def record_approval(
        self,
        submission_id: int,
        approver_name: str,
        decision: str,
        notes: str,
    ) -> ApprovalSubmission | None:
        """Record an approval or rejection for a submission.

        Updates submission status when enough approvals are received or
        when a rejection is recorded.

        Args:
            submission_id: The submission ID.
            approver_name: Name of the approver.
            decision: Decision (approved, rejected).
            notes: Additional notes.

        Returns:
            The updated submission, or None if not found.
        """
        submission = None
        for s in self.submissions:
            if s.submission_id == submission_id:
                submission = s
                break
        if submission is None:
            return None

        submission.approvals.append((approver_name, decision, notes))

        if decision == "rejected":
            submission.status = "rejected"
        elif decision == "approved":
            gate = self.gates.get(submission.gate_name)
            if gate:
                approval_count = sum(1 for _, d, _ in submission.approvals if d == "approved")
                if approval_count >= gate.required_approvers:
                    submission.status = "approved"

        return submission

    def format_status(self) -> str:
        """Format status of all gates and pending submissions.

        Returns:
            Markdown-formatted status report.
        """
        lines = ["## Approval Gates Status", ""]

        if not self.gates:
            lines.append("No approval gates configured.")
            return "\n".join(lines)

        lines.append("### Gates")
        lines.append("")
        for gate in self.gates.values():
            lines.append(f"- **{gate.name}**: {gate.description} (requires {gate.required_approvers} approver(s))")
        lines.append("")

        pending = [s for s in self.submissions if s.status == "pending"]
        if pending:
            lines.append(f"### Pending Submissions ({len(pending)})")
            lines.append("")
            for s in pending:
                approval_count = sum(1 for _, d, _ in s.approvals if d == "approved")
                gate = self.gates.get(s.gate_name)
                required = gate.required_approvers if gate else "?"
                lines.append(f"- **#{s.submission_id}** [{s.risk_level.upper()}] Gate: {s.gate_name} | Approvals: {approval_count}/{required}")
                lines.append(f"  Action: {s.action_description}")
                lines.append(f"  Submitted: {s.submitted_at}")
                if s.approvals:
                    for approver, decision, notes in s.approvals:
                        lines.append(f"  - {approver}: {decision}" + (f" ({notes})" if notes else ""))
                lines.append("")
        else:
            lines.append("### Pending Submissions")
            lines.append("")
            lines.append("No pending submissions.")
            lines.append("")

        return "\n".join(lines)


APPROVAL_GATES_SYSTEM_PROMPT = """## Human-in-the-Loop Approval Gates

You have tools to manage multi-level approval workflows.

**Risk Levels:** low, medium, high, critical
**Decisions:** approved, rejected

**Workflow:**
1. `create_approval_gate` — Define gates with required approver counts
2. `submit_for_approval` — Submit actions to a gate for review
3. `record_approval` — Record approvals or rejections from reviewers
4. `gate_status` — View all gates and pending submissions
5. `clear_gates` — Reset all gates and submissions

A submission is approved when it receives enough approvals from the gate's required count. A single rejection immediately rejects the submission."""


class ApprovalGatesState(TypedDict):
    """State for approval gates middleware."""


class ApprovalGatesMiddleware(AgentMiddleware[ApprovalGatesState, ContextT, ResponseT]):
    """Middleware for human-in-the-loop approval workflows."""

    state_schema = ApprovalGatesState

    def __init__(self) -> None:
        self.store = ApprovalStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build approval gate tools."""
        mw = self

        def create_approval_gate(
            runtime: ToolRuntime[None, ApprovalGatesState],
            name: Annotated[str, "Gate name"],
            required_approvers: Annotated[int, "Number of approvals required"],
            description: Annotated[str, "Description of what this gate controls"],
        ) -> str:
            """Create an approval gate."""
            if name in mw.store.gates:
                return f"Error: Gate '{name}' already exists."
            gate = mw.store.create_gate(
                name=name,
                required_approvers=required_approvers,
                description=description,
            )
            return (
                f"Approval gate '{gate.name}' created: {gate.description} "
                f"(requires {gate.required_approvers} approver(s)). "
                f"Total gates: {len(mw.store.gates)}"
            )

        def submit_for_approval(
            runtime: ToolRuntime[None, ApprovalGatesState],
            gate_name: Annotated[str, "Name of the gate to submit to"],
            action_description: Annotated[str, "Description of the action requiring approval"],
            risk_level: Annotated[str, "Risk level: low, medium, high, critical"] = "medium",
        ) -> str:
            """Submit an action for approval at a gate."""
            if gate_name not in mw.store.gates:
                return f"Error: Gate '{gate_name}' does not exist."
            submission = mw.store.submit(
                gate_name=gate_name,
                action_description=action_description,
                risk_level=risk_level,
            )
            return f"Submission #{submission.submission_id} created at gate '{gate_name}' [{risk_level.upper()}]: {action_description}"

        def record_approval(
            runtime: ToolRuntime[None, ApprovalGatesState],
            submission_id: Annotated[int, "Submission ID"],
            approver_name: Annotated[str, "Name of the approver"],
            decision: Annotated[str, "Decision: approved or rejected"],
            notes: Annotated[str, "Additional notes"] = "",
        ) -> str:
            """Record an approval or rejection for a submission."""
            if decision not in ("approved", "rejected"):
                return "Error: Decision must be 'approved' or 'rejected'."
            submission = mw.store.record_approval(
                submission_id=submission_id,
                approver_name=approver_name,
                decision=decision,
                notes=notes,
            )
            if submission is None:
                return f"Submission #{submission_id} not found."
            result = f"Recorded {decision} from {approver_name} for submission #{submission_id}."
            if submission.status != "pending":
                result += f" Submission is now {submission.status.upper()}."
            return result

        def gate_status(
            runtime: ToolRuntime[None, ApprovalGatesState],
        ) -> str:
            """Check status of all approval gates and pending submissions."""
            return mw.store.format_status()

        def clear_gates(
            runtime: ToolRuntime[None, ApprovalGatesState],
        ) -> str:
            """Clear all gates and submissions."""
            mw.store = ApprovalStore()
            return "All approval gates and submissions cleared."

        return [
            StructuredTool.from_function(
                name="create_approval_gate", description="Create an approval gate with a required number of approvers.", func=create_approval_gate
            ),
            StructuredTool.from_function(
                name="submit_for_approval", description="Submit an action for approval at a gate.", func=submit_for_approval
            ),
            StructuredTool.from_function(
                name="record_approval", description="Record an approval or rejection for a submission.", func=record_approval
            ),
            StructuredTool.from_function(
                name="gate_status", description="Check status of all approval gates and pending submissions.", func=gate_status
            ),
            StructuredTool.from_function(name="clear_gates", description="Clear all gates and submissions.", func=clear_gates),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject approval gates instructions."""
        return request.override(system_message=append_to_system_message(request.system_message, APPROVAL_GATES_SYSTEM_PROMPT))

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


__all__ = ["ApprovalGate", "ApprovalGatesMiddleware", "ApprovalStore", "ApprovalSubmission"]
