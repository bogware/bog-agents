"""Smart Approvals middleware with guardian agent for intelligent tool approval.

Instead of prompting the user for every tool call, a lightweight guardian
sub-agent evaluates whether a tool invocation is safe based on configurable
policies, historical patterns, and risk scoring. Only genuinely risky
operations surface to the human.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

logger = logging.getLogger(__name__)


class RiskLevel(StrEnum):
    """Risk classification for tool invocations."""

    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ApprovalPolicy:
    """A policy rule for auto-approving or escalating tool calls."""

    tool_pattern: str
    """Regex pattern matching tool names."""

    risk_level: RiskLevel = RiskLevel.LOW
    """Default risk level for matching tools."""

    auto_approve: bool = False
    """Whether to auto-approve without guardian review."""

    arg_patterns: dict[str, str] | None = None
    """Argument patterns that modify risk. Keys are arg names, values are
    regex patterns. Matching raises risk by one level."""

    max_auto_approvals_per_minute: int = 30
    """Rate limit for auto-approvals to prevent runaway loops."""

    description: str = ""


@dataclass
class ApprovalDecision:
    """Result of a guardian evaluation."""

    approved: bool
    risk_level: RiskLevel
    reason: str
    tool_name: str
    timestamp: float = field(default_factory=time.time)
    escalated_to_human: bool = False


@dataclass
class ApprovalHistory:
    """Tracks approval decisions for learning patterns."""

    decisions: list[ApprovalDecision] = field(default_factory=list)
    _approval_timestamps: list[float] = field(default_factory=list)

    def record(self, decision: ApprovalDecision) -> None:
        """Record an approval decision."""
        self.decisions.append(decision)
        if decision.approved:
            self._approval_timestamps.append(decision.timestamp)

    def recent_approval_rate(self, window_seconds: float = 60.0) -> int:
        """Count approvals in the last N seconds."""
        cutoff = time.time() - window_seconds
        return sum(1 for t in self._approval_timestamps if t > cutoff)

    def tool_approval_ratio(self, tool_name: str) -> float:
        """Fraction of approvals for a given tool (0.0 to 1.0)."""
        tool_decisions = [d for d in self.decisions if d.tool_name == tool_name]
        if not tool_decisions:
            return 0.5  # No history, neutral
        approved = sum(1 for d in tool_decisions if d.approved)
        return approved / len(tool_decisions)


# Built-in policies for common tools
DEFAULT_POLICIES: list[ApprovalPolicy] = [
    ApprovalPolicy(
        tool_pattern=r"^(read_file|read_many_files|ls|glob|grep|repo_map)$",
        risk_level=RiskLevel.SAFE,
        auto_approve=True,
        description="Read-only operations are always safe",
    ),
    ApprovalPolicy(
        tool_pattern=r"^(write_todos|show_cost|show_context|detect_project)$",
        risk_level=RiskLevel.SAFE,
        auto_approve=True,
        description="Informational tools are always safe",
    ),
    ApprovalPolicy(
        tool_pattern=r"^(write_file|edit_file|multi_edit)$",
        risk_level=RiskLevel.LOW,
        auto_approve=False,
        description="File mutations need guardian review",
    ),
    ApprovalPolicy(
        tool_pattern=r"^execute$",
        risk_level=RiskLevel.MEDIUM,
        auto_approve=False,
        arg_patterns={
            "command": r"(rm\s|sudo\s|chmod\s|chown\s|dd\s|mkfs|>\s*/dev/)",
        },
        description="Shell execution; dangerous patterns raise risk",
    ),
    ApprovalPolicy(
        tool_pattern=r"^(git_commit|git_push|git_reset|git_stash)$",
        risk_level=RiskLevel.MEDIUM,
        auto_approve=False,
        description="Git mutations need review",
    ),
    ApprovalPolicy(
        tool_pattern=r"^(git_status|git_diff|git_log|git_show|git_blame)$",
        risk_level=RiskLevel.SAFE,
        auto_approve=True,
        description="Read-only git operations",
    ),
]


def _classify_risk(
    tool_name: str,
    tool_args: dict[str, Any],
    policies: list[ApprovalPolicy],
) -> tuple[RiskLevel, ApprovalPolicy | None]:
    """Classify a tool call's risk level based on policies.

    Args:
        tool_name: Name of the tool being called.
        tool_args: Arguments to the tool.
        policies: Active approval policies.

    Returns:
        Tuple of (risk_level, matching_policy).
    """
    for policy in policies:
        if re.match(policy.tool_pattern, tool_name):
            risk = policy.risk_level
            # Check if args escalate risk
            if policy.arg_patterns:
                for arg_name, pattern in policy.arg_patterns.items():
                    arg_value = str(tool_args.get(arg_name, ""))
                    if re.search(pattern, arg_value):
                        risk_levels = list(RiskLevel)
                        idx = risk_levels.index(risk)
                        if idx < len(risk_levels) - 1:
                            risk = risk_levels[idx + 1]
                        logger.debug(
                            "Risk escalated to %s for %s due to arg %s matching %s",
                            risk,
                            tool_name,
                            arg_name,
                            pattern,
                        )
                        break
            return risk, policy
    return RiskLevel.MEDIUM, None


def evaluate_tool_call(
    tool_name: str,
    tool_args: dict[str, Any],
    policies: list[ApprovalPolicy],
    history: ApprovalHistory,
    *,
    auto_approve_threshold: RiskLevel = RiskLevel.LOW,
    require_human_above: RiskLevel = RiskLevel.HIGH,
) -> ApprovalDecision:
    """Evaluate whether a tool call should be approved.

    The guardian logic:
    1. Classify the risk level based on policies
    2. Auto-approve SAFE and LOW risk (configurable threshold)
    3. Use historical patterns for MEDIUM risk
    4. Escalate HIGH/CRITICAL to human

    Args:
        tool_name: Name of the tool.
        tool_args: Tool arguments.
        policies: Active approval policies.
        history: Approval history for pattern learning.
        auto_approve_threshold: Maximum risk level for auto-approval.
        require_human_above: Minimum risk level requiring human approval.

    Returns:
        ApprovalDecision with the verdict.
    """
    risk, policy = _classify_risk(tool_name, tool_args, policies)

    # Rate limiting check
    if history.recent_approval_rate() > (policy.max_auto_approvals_per_minute if policy else 30):
        return ApprovalDecision(
            approved=False,
            risk_level=risk,
            reason="Rate limit exceeded for auto-approvals",
            tool_name=tool_name,
            escalated_to_human=True,
        )

    # SAFE tools with auto_approve policy
    if policy and policy.auto_approve and risk == RiskLevel.SAFE:
        decision = ApprovalDecision(
            approved=True,
            risk_level=risk,
            reason=f"Auto-approved: {policy.description}",
            tool_name=tool_name,
        )
        history.record(decision)
        return decision

    # Below threshold: auto-approve
    risk_levels = list(RiskLevel)
    if risk_levels.index(risk) <= risk_levels.index(auto_approve_threshold):
        # Check historical approval ratio for this tool
        ratio = history.tool_approval_ratio(tool_name)
        if ratio >= 0.8:
            decision = ApprovalDecision(
                approved=True,
                risk_level=risk,
                reason=f"Auto-approved: risk={risk}, historical approval ratio={ratio:.0%}",
                tool_name=tool_name,
            )
            history.record(decision)
            return decision

    # Above human-required threshold: escalate
    if risk_levels.index(risk) >= risk_levels.index(require_human_above):
        return ApprovalDecision(
            approved=False,
            risk_level=risk,
            reason=f"Human approval required: risk={risk}",
            tool_name=tool_name,
            escalated_to_human=True,
        )

    # MEDIUM risk: use guardian heuristics
    ratio = history.tool_approval_ratio(tool_name)
    if ratio >= 0.9 and len([d for d in history.decisions if d.tool_name == tool_name]) >= 3:
        decision = ApprovalDecision(
            approved=True,
            risk_level=risk,
            reason=f"Guardian auto-approved: consistent approval history ({ratio:.0%} over "
            f"{len([d for d in history.decisions if d.tool_name == tool_name])} calls)",
            tool_name=tool_name,
        )
        history.record(decision)
        return decision

    return ApprovalDecision(
        approved=False,
        risk_level=risk,
        reason=f"Guardian review required: risk={risk}, approval_ratio={ratio:.0%}",
        tool_name=tool_name,
        escalated_to_human=True,
    )


class SmartApprovalsMiddleware(AgentMiddleware):
    """Middleware that intelligently gates tool approval.

    Uses a guardian agent pattern to evaluate tool calls against configurable
    policies, historical patterns, and risk scoring. Only genuinely risky
    operations are escalated to the human for approval.

    Example:
        ```python
        from bog_agents.middleware.smart_approvals import (
            SmartApprovalsMiddleware,
            ApprovalPolicy,
            RiskLevel,
        )

        middleware = SmartApprovalsMiddleware(
            policies=[
                ApprovalPolicy(
                    tool_pattern=r"^execute$",
                    risk_level=RiskLevel.HIGH,
                    arg_patterns={"command": r"rm\\s+-rf"},
                ),
            ],
            auto_approve_threshold=RiskLevel.LOW,
        )

        agent = create_agent(middleware=[middleware])
        ```
    """

    policies: list[ApprovalPolicy]
    history: ApprovalHistory
    auto_approve_threshold: RiskLevel
    require_human_above: RiskLevel

    def __init__(
        self,
        *,
        policies: list[ApprovalPolicy] | None = None,
        include_defaults: bool = True,
        auto_approve_threshold: RiskLevel = RiskLevel.LOW,
        require_human_above: RiskLevel = RiskLevel.HIGH,
    ) -> None:
        """Initialize the smart approvals middleware.

        Args:
            policies: Custom approval policies. Evaluated in order.
            include_defaults: Whether to include default safe-tool policies.
            auto_approve_threshold: Max risk level for auto-approval.
            require_human_above: Min risk level requiring human approval.
        """
        all_policies: list[ApprovalPolicy] = []
        if include_defaults:
            all_policies.extend(DEFAULT_POLICIES)
        if policies:
            all_policies.extend(policies)

        self.policies = all_policies
        self.history = ApprovalHistory()
        self.auto_approve_threshold = auto_approve_threshold
        self.require_human_above = require_human_above

    def evaluate(self, tool_name: str, tool_args: dict[str, Any]) -> ApprovalDecision:
        """Evaluate a tool call for approval.

        Args:
            tool_name: Name of the tool being invoked.
            tool_args: Arguments to the tool.

        Returns:
            ApprovalDecision with the verdict.
        """
        return evaluate_tool_call(
            tool_name,
            tool_args,
            self.policies,
            self.history,
            auto_approve_threshold=self.auto_approve_threshold,
            require_human_above=self.require_human_above,
        )

    def record_human_decision(self, tool_name: str, approved: bool, risk_level: RiskLevel | None = None) -> None:
        """Record a human's approval/denial decision for learning.

        Args:
            tool_name: Name of the tool.
            approved: Whether the human approved.
            risk_level: Risk level (uses MEDIUM if not provided).
        """
        decision = ApprovalDecision(
            approved=approved,
            risk_level=risk_level or RiskLevel.MEDIUM,
            reason="Human decision",
            tool_name=tool_name,
            escalated_to_human=True,
        )
        self.history.record(decision)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
    ) -> ModelResponse[ResponseT]:
        """Pass through model calls — approval happens at tool execution time."""
        return await call_next(request)
