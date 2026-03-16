"""Code intelligence CLI interface.

Feature #59: Agent replay & debugging.
Feature #60: Codebase health score.
Feature #61: Migration assistant.
Feature #62: Documentation generator.
Feature #63: Onboarding mode.
Feature #64: Performance profiler integration.
Feature #65: Database schema tools.
Feature #66: Infrastructure as code.
Feature #67: Changelog generator.
Feature #68: Code transformation engine.
Feature #69: Smart imports.
Feature #70: Cross-repo operations.
Feature #71: Time-travel debugging.
Feature #74: Agent-to-Agent protocol.
Feature #75: Offline mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HealthReport:
    """Codebase health report."""

    overall_score: float = 0.0
    files_analyzed: int = 0
    total_lines: int = 0
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def parse_health_command(text: str) -> dict[str, str]:
    """Parse a /health command.

    Subcommands:
    - /health — full health report
    - /health quick — quick summary
    - /health detail <area> — detailed area report

    Args:
        text: Command text.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else "full"
    arg = parts[1] if len(parts) > 1 else ""
    return {"action": action, "argument": arg}


def parse_migrate_command(text: str) -> dict[str, str]:
    """Parse a /migrate command.

    Format: /migrate <from> <to>

    Args:
        text: Command text.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split()
    from_tech = parts[0] if parts else ""
    to_tech = parts[1] if len(parts) > 1 else ""
    return {"from": from_tech, "to": to_tech}


def parse_changelog_command(text: str) -> dict[str, str]:
    """Parse a /changelog command.

    Format: /changelog [since_ref]

    Args:
        text: Command text.

    Returns:
        Parsed command dict.
    """
    ref = text.strip() or "HEAD~20"
    return {"since_ref": ref}


def generate_health_prompt(paths: list[str] | None = None) -> str:
    """Generate a prompt for health analysis.

    Args:
        paths: Optional paths to analyze.

    Returns:
        Prompt for the agent.
    """
    path_str = ", ".join(paths) if paths else "the entire codebase"
    return (
        f"Analyze the health of {path_str}.\n"
        f"Report on:\n"
        f"1. Code complexity (functions >50 lines, deep nesting)\n"
        f"2. Documentation coverage (docstrings, comments)\n"
        f"3. Test coverage\n"
        f"4. Dependency freshness\n"
        f"5. Code duplication\n"
        f"6. Security concerns\n"
        f"Provide an overall score out of 100 and specific recommendations.\n"
    )


def generate_migration_prompt(from_tech: str, to_tech: str) -> str:
    """Generate a prompt for migration planning.

    Args:
        from_tech: Source technology.
        to_tech: Target technology.

    Returns:
        Prompt for the agent.
    """
    return (
        f"Create a detailed migration plan from {from_tech} to {to_tech}.\n"
        f"For each step:\n"
        f"1. Describe the change\n"
        f"2. List affected files\n"
        f"3. Note any breaking changes\n"
        f"4. Provide code examples\n"
        f"5. Include rollback instructions\n"
    )


def generate_onboard_prompt() -> str:
    """Generate a prompt for codebase onboarding.

    Returns:
        Prompt for the agent.
    """
    return (
        "Give me an interactive onboarding tour of this codebase.\n"
        "Cover:\n"
        "1. Project purpose and tech stack\n"
        "2. Directory structure and key files\n"
        "3. Architecture and design patterns\n"
        "4. How to build, test, and run\n"
        "5. Coding conventions and style\n"
        "6. Common workflows (PR, deploy, etc.)\n"
        "7. Where to find things\n"
    )


def generate_docs_prompt(scope: str = "api") -> str:
    """Generate a prompt for documentation generation.

    Args:
        scope: Documentation scope: 'api', 'readme', 'architecture'.

    Returns:
        Prompt for the agent.
    """
    scopes = {
        "api": "Generate API documentation for all public functions, classes, and modules.",
        "readme": "Generate a comprehensive README.md with project overview, setup, usage, and examples.",
        "architecture": "Generate an architecture document describing the system design, components, and data flow.",
    }
    return scopes.get(scope, scopes["api"])


def generate_infra_prompt(infra_type: str, description: str) -> str:
    """Generate a prompt for infrastructure generation.

    Args:
        infra_type: Type of infrastructure.
        description: Description of needs.

    Returns:
        Prompt for the agent.
    """
    return (
        f"Generate a {infra_type} configuration for:\n{description}\n"
        f"Follow best practices for production deployments.\n"
        f"Include comments explaining key decisions.\n"
    )
