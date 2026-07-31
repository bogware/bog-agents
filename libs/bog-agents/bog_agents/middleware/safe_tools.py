"""Middleware for selective per-tool auto-approval (SafeTools).

Feature #37: SafeTools / selective auto-approval — allows specific tools
or tool patterns to bypass HITL confirmation while keeping others gated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bog_agents.exec_risk import command_has_exec_risk

logger = logging.getLogger(__name__)


__all__ = [
    "SafeToolRule",
    "SafeToolsConfig",
]


@dataclass
class SafeToolRule:
    """A rule defining which tool calls can be auto-approved."""

    tool_name: str | None = None
    """Exact tool name to match. If None, uses pattern matching."""

    tool_pattern: str | None = None
    """Regex pattern to match tool names."""

    arg_constraints: dict[str, Any] | None = None
    """Optional constraints on tool arguments. Keys are arg names, values are
    allowed values or patterns."""

    description: str = ""
    """Human-readable description of this rule."""


@dataclass
class SafeToolsConfig:
    """Configuration for selective tool auto-approval.

    Example configuration:
    ```python
    config = SafeToolsConfig(
        rules=[
            SafeToolRule(tool_name="ls", description="Always allow listing files"),
            SafeToolRule(tool_name="read_file", description="Always allow reading"),
            SafeToolRule(tool_name="glob", description="Always allow file search"),
            SafeToolRule(tool_name="grep", description="Always allow text search"),
            SafeToolRule(
                tool_name="execute",
                arg_constraints={"command": r"^(pytest|ruff|make)\\s"},
                description="Allow test/lint commands",
            ),
            SafeToolRule(
                tool_pattern=r"git_(status|diff|log|show|blame)",
                description="Allow read-only git operations",
            ),
        ]
    )
    ```
    """

    rules: list[SafeToolRule] = field(default_factory=list)
    """Rules defining which tools can be auto-approved."""

    default_safe_tools: bool = True
    """Whether to include default safe tool rules (read-only tools)."""

    def __post_init__(self) -> None:
        """Add default safe tools if enabled."""
        if self.default_safe_tools and not any(r.tool_name == "ls" for r in self.rules):
            default_rules = [
                SafeToolRule(tool_name="ls", description="List directory contents"),
                SafeToolRule(tool_name="read_file", description="Read file contents"),
                SafeToolRule(tool_name="read_many_files", description="Read multiple files"),
                SafeToolRule(tool_name="glob", description="Find files by pattern"),
                SafeToolRule(tool_name="grep", description="Search file contents"),
                SafeToolRule(tool_name="repo_map", description="View repository structure"),
                SafeToolRule(tool_name="detect_project", description="Detect project type"),
                SafeToolRule(tool_name="show_cost", description="View cost information"),
                SafeToolRule(tool_name="show_context", description="View context usage"),
                SafeToolRule(tool_name="write_todos", description="Manage todo list"),
                SafeToolRule(
                    tool_pattern=r"git_(status|diff|log|show|blame|branch)",
                    description="Read-only git operations",
                ),
            ]
            self.rules = default_rules + self.rules


def is_tool_safe(
    tool_name: str,
    tool_args: dict[str, Any],
    config: SafeToolsConfig,
) -> bool:
    """Check if a tool call matches any safe tool rule.

    Args:
        tool_name: Name of the tool being called.
        tool_args: Arguments passed to the tool.
        config: SafeTools configuration with rules.

    Returns:
        True if the tool call is considered safe and can be auto-approved.
    """
    for rule in config.rules:
        # Check tool name match
        if rule.tool_name and rule.tool_name != tool_name:
            continue
        if rule.tool_pattern and not re.match(rule.tool_pattern, tool_name):
            continue
        if not rule.tool_name and not rule.tool_pattern:
            continue

        # Check argument constraints
        if rule.arg_constraints:
            all_match = True
            for arg_name, constraint in rule.arg_constraints.items():
                arg_value = str(tool_args.get(arg_name, ""))
                if isinstance(constraint, str):
                    if not re.match(constraint, arg_value):
                        all_match = False
                        break
                elif arg_value != str(constraint):
                    all_match = False
                    break
            if not all_match:
                continue

        # Exec-risk veto (Tier-1 #2): a shell command that *looks* read-only can
        # still execute attacker-controlled code via a flag/config (e.g.
        # `git -c core.fsmonitor=…`, `sort --compress-program=…`). Never
        # auto-approve those — fall through to human-in-the-loop even if a rule
        # matched. Only applies to command-bearing tool calls.
        command = tool_args.get("command")
        if isinstance(command, str) and command_has_exec_risk(command):
            logger.info("SafeTools: refusing auto-approval — exec-risk in command: %s", command)
            return False

        return True

    return False


def load_safe_tools_config(config_data: dict[str, Any]) -> SafeToolsConfig:
    """Load SafeTools configuration from a dict (e.g., from config.toml).

    Args:
        config_data: Configuration dictionary with 'safe_tools' key.

    Returns:
        SafeToolsConfig instance.

    Example config.toml:
    ```toml
    [safe_tools]
    default_safe_tools = true

    [[safe_tools.rules]]
    tool_name = "execute"
    description = "Allow test commands"
    [safe_tools.rules.arg_constraints]
    command = "^pytest"
    ```
    """
    safe_tools_data = config_data.get("safe_tools", {})

    rules = []
    for rule_data in safe_tools_data.get("rules", []):
        rules.append(
            SafeToolRule(
                tool_name=rule_data.get("tool_name"),
                tool_pattern=rule_data.get("tool_pattern"),
                arg_constraints=rule_data.get("arg_constraints"),
                description=rule_data.get("description", ""),
            )
        )

    return SafeToolsConfig(
        rules=rules,
        default_safe_tools=safe_tools_data.get("default_safe_tools", True),
    )
