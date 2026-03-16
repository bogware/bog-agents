"""Enterprise and team features CLI interface.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TeamMember:
    """A team member with role assignment."""

    name: str
    role: str = "developer"
    email: str = ""


@dataclass
class TeamSettings:
    """Team-level settings."""

    name: str = ""
    members: list[TeamMember] = field(default_factory=list)
    shared_mcp_servers: list[dict[str, str]] = field(default_factory=list)
    compliance_policies: list[dict[str, str]] = field(default_factory=list)
    sso_provider: str = ""  # saml, oidc, none


def get_team_config_path(working_dir: Path | None = None) -> Path:
    """Get the team configuration file path.

    Args:
        working_dir: Working directory.

    Returns:
        Path to team config file.
    """
    directory = working_dir or Path.cwd()
    return directory / ".bog-agents" / "team.json"


def load_team_settings(config_path: Path) -> TeamSettings:
    """Load team settings from config file.

    Args:
        config_path: Path to team config.

    Returns:
        Parsed team settings.
    """
    if not config_path.exists():
        return TeamSettings()

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        settings = TeamSettings(name=data.get("name", ""))
        for member_data in data.get("members", []):
            settings.members.append(TeamMember(**member_data))
        settings.shared_mcp_servers = data.get("shared_mcp_servers", [])
        settings.compliance_policies = data.get("compliance_policies", [])
        settings.sso_provider = data.get("sso_provider", "")
        return settings
    except (json.JSONDecodeError, OSError, TypeError):
        return TeamSettings()


def save_team_settings(settings: TeamSettings, config_path: Path) -> None:
    """Save team settings to config file.

    Args:
        settings: Team settings to save.
        config_path: Path to save to.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "name": settings.name,
        "members": [
            {"name": m.name, "role": m.role, "email": m.email} for m in settings.members
        ],
        "shared_mcp_servers": settings.shared_mcp_servers,
        "compliance_policies": settings.compliance_policies,
        "sso_provider": settings.sso_provider,
    }
    config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def parse_team_command(text: str) -> dict[str, str]:
    """Parse a /team command.

    Subcommands:
    - /team — show team settings
    - /team roles — list roles
    - /team audit — view audit log
    - /team analytics — view usage analytics
    - /team policies — list compliance policies

    Args:
        text: Command text after /team.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else "show"
    arg = parts[1] if len(parts) > 1 else ""
    return {"action": action, "argument": arg}


def format_team_settings(settings: TeamSettings) -> str:
    """Format team settings for display.

    Args:
        settings: Team settings.

    Returns:
        Formatted string.
    """
    lines = [f"Team: {settings.name or '(unnamed)'}"]

    if settings.members:
        lines.append(f"\nMembers ({len(settings.members)}):")
        for m in settings.members:
            lines.append(f"  {m.name} [{m.role}]")
    else:
        lines.append("\nNo members configured.")

    if settings.sso_provider:
        lines.append(f"\nSSO Provider: {settings.sso_provider}")

    if settings.compliance_policies:
        lines.append(f"\nPolicies ({len(settings.compliance_policies)}):")
        for p in settings.compliance_policies:
            lines.append(f"  {p.get('name', 'unnamed')}: {p.get('description', '')}")

    return "\n".join(lines)
