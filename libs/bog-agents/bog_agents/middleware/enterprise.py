"""Middleware for enterprise and team features.

Feature #51: Team configuration.
Feature #52: Usage analytics dashboard.
Feature #53: Audit logging.
Feature #54: Role-based permissions.
Feature #55: SSO/SAML integration.
Feature #56: Compliance policies.
Feature #57: Config change hooks.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    """An audit log entry."""

    timestamp: float
    action: str
    tool: str
    details: str
    user: str = "agent"
    risk_level: str = "low"  # low, medium, high


@dataclass
class CompliancePolicy:
    """A compliance policy rule."""

    name: str
    description: str
    rule_type: str  # deny_tool, deny_domain, require_approval, read_only
    pattern: str  # tool name, domain, or file pattern
    enabled: bool = True


@dataclass
class TeamRole:
    """A team role with permissions."""

    name: str
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    max_budget_usd: float = 0.0
    can_execute: bool = True
    can_write_files: bool = True


@dataclass
class UsageRecord:
    """A usage tracking record."""

    timestamp: float
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    model: str = ""


class TeamConfig:
    """Team-level configuration."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.roles: dict[str, TeamRole] = {
            "admin": TeamRole(name="admin", can_execute=True, can_write_files=True),
            "developer": TeamRole(name="developer", can_execute=True, can_write_files=True),
            "reviewer": TeamRole(
                name="reviewer",
                can_execute=False,
                can_write_files=False,
                denied_tools=["execute", "write_file", "edit_file"],
            ),
            "viewer": TeamRole(
                name="viewer",
                can_execute=False,
                can_write_files=False,
                denied_tools=["execute", "write_file", "edit_file", "git_commit"],
            ),
        }
        self.policies: list[CompliancePolicy] = []
        self.shared_mcp_servers: list[dict[str, str]] = []

    def load(self) -> None:
        """Load team config from disk."""
        if self.config_path and self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                for name, role_data in data.get("roles", {}).items():
                    self.roles[name] = TeamRole(**role_data)
                for policy_data in data.get("policies", []):
                    self.policies.append(CompliancePolicy(**policy_data))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                logger.warning("Failed to load team config: %s", e)

    def save(self) -> None:
        """Save team config to disk."""
        if self.config_path:
            data: dict[str, Any] = {
                "roles": {},
                "policies": [],
            }
            for name, role in self.roles.items():
                data["roles"][name] = {
                    "name": role.name,
                    "allowed_tools": role.allowed_tools,
                    "denied_tools": role.denied_tools,
                    "max_budget_usd": role.max_budget_usd,
                    "can_execute": role.can_execute,
                    "can_write_files": role.can_write_files,
                }
            for policy in self.policies:
                data["policies"].append(
                    {
                        "name": policy.name,
                        "description": policy.description,
                        "rule_type": policy.rule_type,
                        "pattern": policy.pattern,
                        "enabled": policy.enabled,
                    }
                )
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except OSError as e:
                logger.warning("Failed to save team config: %s", e)


class EnterpriseState(TypedDict):
    """State for enterprise middleware."""


class EnterpriseMiddleware(AgentMiddleware[EnterpriseState, ContextT, ResponseT]):
    """Middleware for enterprise team features.

    Provides audit logging, role-based access, compliance policies,
    usage tracking, and team configuration management.

    Args:
        working_dir: Project root directory.
        team_config_path: Path to team configuration file.
        current_role: Role of the current user.
        audit_log_path: Path to write audit logs.
    """

    state_schema = EnterpriseState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        team_config_path: Path | None = None,
        current_role: str = "developer",
        audit_log_path: Path | None = None,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._team_config = TeamConfig(team_config_path)
        self._team_config.load()
        self._current_role = current_role
        self._audit_log: list[AuditEntry] = []
        self._audit_path = audit_log_path
        self._usage: list[UsageRecord] = []
        self.tools = self._build_tools()

    def log_action(self, action: str, tool: str, details: str, risk_level: str = "low") -> None:
        """Log an action to the audit trail.

        Args:
            action: Description of the action.
            tool: Tool that performed the action.
            details: Additional details.
            risk_level: Risk level of the action.
        """
        entry = AuditEntry(
            timestamp=time.time(),
            action=action,
            tool=tool,
            details=details,
            risk_level=risk_level,
        )
        self._audit_log.append(entry)
        if self._audit_path:
            try:
                with self._audit_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"timestamp": entry.timestamp, "action": action, "tool": tool, "details": details, "risk": risk_level}) + "\n")
            except OSError:
                pass

    def check_permission(self, tool_name: str) -> bool:
        """Check if current role has permission for a tool.

        Args:
            tool_name: Tool name to check.

        Returns:
            True if permitted.
        """
        role = self._team_config.roles.get(self._current_role)
        if role is None:
            return True
        if tool_name in role.denied_tools:
            return False
        if role.allowed_tools and tool_name not in role.allowed_tools:
            return False
        return True

    def check_policy(self, action: str, target: str) -> str | None:
        """Check compliance policies.

        Args:
            action: Action being performed.
            target: Target of the action.

        Returns:
            Error message if policy blocks the action, None if allowed.
        """
        for policy in self._team_config.policies:
            if not policy.enabled:
                continue
            if policy.rule_type == "deny_tool" and policy.pattern == action:
                return f"Blocked by policy '{policy.name}': {policy.description}"
            if policy.rule_type == "deny_domain" and policy.pattern in target:
                return f"Blocked by policy '{policy.name}': {policy.description}"
        return None

    def _build_tools(self) -> list[BaseTool]:
        """Build enterprise tools."""
        middleware = self

        def view_audit_log(
            runtime: ToolRuntime[None, EnterpriseState],
            count: Annotated[int, "Number of recent entries to show"] = 20,
            risk_level: Annotated[str, "Filter by risk level: 'all', 'low', 'medium', 'high'"] = "all",
        ) -> str:
            """View the audit log of all agent actions."""
            entries = middleware._audit_log
            if risk_level != "all":
                entries = [e for e in entries if e.risk_level == risk_level]
            entries = entries[-count:]
            if not entries:
                return "No audit entries found."
            lines = [f"Audit Log (last {len(entries)} entries):"]
            for e in entries:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.timestamp))
                lines.append(f"  [{ts}] [{e.risk_level}] {e.action} ({e.tool}): {e.details[:80]}")
            return "\n".join(lines)

        def view_usage(
            runtime: ToolRuntime[None, EnterpriseState],
        ) -> str:
            """View usage analytics for the current session."""
            if not middleware._usage:
                return "No usage data recorded yet."
            total_in = sum(r.tokens_in for r in middleware._usage)
            total_out = sum(r.tokens_out for r in middleware._usage)
            total_cost = sum(r.cost_usd for r in middleware._usage)
            total_calls = sum(r.tool_calls for r in middleware._usage)
            return (
                f"Usage Analytics:\n"
                f"  Total requests: {len(middleware._usage)}\n"
                f"  Tokens in: {total_in:,}\n"
                f"  Tokens out: {total_out:,}\n"
                f"  Total cost: ${total_cost:.4f}\n"
                f"  Tool calls: {total_calls}"
            )

        def manage_team_roles(
            runtime: ToolRuntime[None, EnterpriseState],
            action: Annotated[str, "'list', 'show', or 'set_role'"] = "list",
            role_name: str = "",
        ) -> str:
            """Manage team roles and permissions."""
            if action == "list":
                lines = ["Team Roles:"]
                for name, role in middleware._team_config.roles.items():
                    marker = " *" if name == middleware._current_role else ""
                    lines.append(f"  {name}{marker}: execute={role.can_execute}, write={role.can_write_files}")
                return "\n".join(lines)
            if action == "show" and role_name:
                role = middleware._team_config.roles.get(role_name)
                if not role:
                    return f"Role '{role_name}' not found."
                return (
                    f"Role: {role.name}\n"
                    f"  Can execute: {role.can_execute}\n"
                    f"  Can write files: {role.can_write_files}\n"
                    f"  Allowed tools: {role.allowed_tools or 'all'}\n"
                    f"  Denied tools: {role.denied_tools or 'none'}\n"
                    f"  Budget limit: ${role.max_budget_usd:.2f}"
                )
            return "Use action='list' or action='show' with role_name."

        def manage_policies(
            runtime: ToolRuntime[None, EnterpriseState],
            action: Annotated[str, "'list', 'add', 'remove', or 'toggle'"] = "list",
            name: str = "",
            description: str = "",
            rule_type: str = "",
            pattern: str = "",
        ) -> str:
            """Manage compliance policies."""
            if action == "list":
                if not middleware._team_config.policies:
                    return "No compliance policies configured."
                lines = ["Compliance Policies:"]
                for p in middleware._team_config.policies:
                    status = "enabled" if p.enabled else "disabled"
                    lines.append(f"  [{status}] {p.name}: {p.description} ({p.rule_type}: {p.pattern})")
                return "\n".join(lines)
            if action == "add":
                policy = CompliancePolicy(name=name, description=description, rule_type=rule_type, pattern=pattern)
                middleware._team_config.policies.append(policy)
                middleware._team_config.save()
                return f"Added policy '{name}'"
            if action == "toggle" and name:
                for p in middleware._team_config.policies:
                    if p.name == name:
                        p.enabled = not p.enabled
                        middleware._team_config.save()
                        status = "enabled" if p.enabled else "disabled"
                        return f"Policy '{name}' is now {status}"
                return f"Policy '{name}' not found."
            return "Use action='list', 'add', or 'toggle'."

        def export_analytics(
            runtime: ToolRuntime[None, EnterpriseState],
            format: Annotated[str, "Export format: 'json' or 'csv'"] = "json",  # noqa: A002
        ) -> str:
            """Export usage analytics data."""
            if not middleware._usage:
                return "No usage data to export."
            if format == "csv":
                lines = ["timestamp,tokens_in,tokens_out,cost_usd,tool_calls,model"]
                for r in middleware._usage:
                    lines.append(f"{r.timestamp},{r.tokens_in},{r.tokens_out},{r.cost_usd},{r.tool_calls},{r.model}")
                return "\n".join(lines)
            records = [
                {
                    "timestamp": r.timestamp,
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                    "cost_usd": r.cost_usd,
                    "tool_calls": r.tool_calls,
                    "model": r.model,
                }
                for r in middleware._usage
            ]
            return json.dumps(records, indent=2)

        return [
            StructuredTool.from_function(name="audit_log", description="View audit log.", func=view_audit_log),
            StructuredTool.from_function(name="usage_analytics", description="View usage analytics.", func=view_usage),
            StructuredTool.from_function(name="team_roles", description="Manage team roles.", func=manage_team_roles),
            StructuredTool.from_function(name="compliance_policies", description="Manage policies.", func=manage_policies),
            StructuredTool.from_function(name="export_analytics", description="Export analytics data.", func=export_analytics),
        ]
