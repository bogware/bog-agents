"""Recommendation engine for bog-agents CLI.

Provides structured code review and recommendations with customizable
persona, focus areas, scope, and clarifying-question behavior.

Usage:
    /recommend                          — Use defaults (balanced, full project)
    /recommend --focus security         — Focus on security
    /recommend --persona architect      — Senior architect persona
    /recommend --scope libs/bog-agents  — Scope to a directory
    /recommend --questions 5            — Ask 5 clarifying questions first
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class Persona(StrEnum):
    """Review persona that shapes the recommendation style."""

    ARCHITECT = "architect"
    SECURITY = "security"
    PERFORMANCE = "performance"
    DX = "dx"
    BALANCED = "balanced"


class Focus(StrEnum):
    """Primary focus area for the review."""

    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PERFORMANCE = "performance"
    TESTING = "testing"
    CODE_QUALITY = "code_quality"
    DOCUMENTATION = "documentation"
    DEPENDENCIES = "dependencies"
    DEPLOYMENT = "deployment"
    GENERAL = "general"


class Scope(StrEnum):
    """Review scope level."""

    FILE = "file"
    DIRECTORY = "directory"
    PACKAGE = "package"
    PROJECT = "project"


PERSONA_SYSTEM_PROMPTS: dict[Persona, str] = {
    Persona.ARCHITECT: (
        "You are a Senior Principal Architect reviewing this codebase. "
        "Focus on system design, abstractions, coupling, cohesion, scalability, "
        "and long-term maintainability. Be opinionated about patterns and anti-patterns. "
        "Evaluate whether the architecture will hold up under growth and team scaling."
    ),
    Persona.SECURITY: (
        "You are a Security Engineer performing a thorough security review. "
        "Focus on OWASP Top 10, input validation, authentication/authorization, "
        "secrets management, dependency vulnerabilities, injection flaws, "
        "and data protection. Flag any issue that could be exploited."
    ),
    Persona.PERFORMANCE: (
        "You are a Performance Engineer reviewing this codebase. "
        "Focus on algorithmic complexity, memory usage, I/O efficiency, "
        "caching opportunities, database query patterns, and concurrency. "
        "Identify bottlenecks and suggest measurable improvements."
    ),
    Persona.DX: (
        "You are a Developer Experience lead reviewing this codebase. "
        "Focus on ergonomics, API clarity, error messages, documentation quality, "
        "onboarding friction, IDE support, and developer productivity. "
        "The goal is making this codebase a joy to work with."
    ),
    Persona.BALANCED: (
        "You are an experienced Staff Engineer performing a comprehensive review. "
        "Balance architecture, security, performance, and code quality concerns. "
        "Prioritize issues by real-world impact and provide actionable recommendations."
    ),
}


FOCUS_INSTRUCTIONS: dict[Focus, str] = {
    Focus.ARCHITECTURE: (
        "Analyze the overall architecture: module boundaries, dependency flow, "
        "abstraction layers, coupling between components, and extensibility points. "
        "Evaluate naming consistency and whether the structure communicates intent."
    ),
    Focus.SECURITY: (
        "Scan for security vulnerabilities: hardcoded secrets, injection vectors, "
        "insecure deserialization, path traversal, SSRF, broken auth, "
        "and dependency CVEs. Check for proper input validation at system boundaries."
    ),
    Focus.PERFORMANCE: (
        "Profile for performance issues: O(n^2) algorithms, unnecessary allocations, "
        "blocking I/O in async code, missing caching, N+1 query patterns, "
        "and oversized payloads. Suggest concrete optimizations with expected impact."
    ),
    Focus.TESTING: (
        "Evaluate test coverage and quality: identify untested code paths, "
        "fragile tests, missing edge cases, test isolation issues, and "
        "opportunities for property-based or integration tests."
    ),
    Focus.CODE_QUALITY: (
        "Review code quality: readability, naming, consistency, dead code, "
        "unnecessary complexity, duplicated logic, type safety, error handling, "
        "and adherence to project conventions."
    ),
    Focus.DOCUMENTATION: (
        "Assess documentation completeness: missing docstrings, outdated comments, "
        "README accuracy, API reference gaps, and onboarding documentation. "
        "Evaluate whether a new developer could understand the codebase."
    ),
    Focus.DEPENDENCIES: (
        "Review the dependency landscape: outdated packages, unused dependencies, "
        "version pinning strategy, transitive dependency risks, license compliance, "
        "and opportunities to reduce the dependency tree."
    ),
    Focus.DEPLOYMENT: (
        "Evaluate deployment readiness: CI/CD configuration, environment variable "
        "handling, health checks, graceful shutdown, logging/observability, "
        "container configuration, and production hardening."
    ),
    Focus.GENERAL: (
        "Perform a general review covering architecture, code quality, testing, "
        "and any issues that stand out. Prioritize findings by severity and impact."
    ),
}


@dataclass
class RecommendConfig:
    """Configuration for a recommendation review.

    Attributes:
        persona: Review persona that shapes the recommendation style.
        focus: Primary focus area for the review.
        scope: What to review — a file, directory, or full project.
        scope_path: Specific path when scope is FILE or DIRECTORY.
        num_questions: Number of clarifying questions to ask before reviewing.
        include_examples: Include code examples in recommendations.
        severity_threshold: Minimum severity to report (low/medium/high/critical).
        max_findings: Maximum number of findings to report.
        output_format: Output format (text, markdown, json).
    """

    persona: Persona = Persona.BALANCED
    focus: Focus = Focus.GENERAL
    scope: Scope = Scope.PROJECT
    scope_path: str = "."
    num_questions: int = 3
    include_examples: bool = True
    severity_threshold: str = "low"
    max_findings: int = 25
    output_format: str = "markdown"


def parse_recommend_args(args_str: str) -> RecommendConfig:
    """Parse /recommend command arguments into a config.

    Supports flags:
        --persona <name>      — architect, security, performance, dx, balanced
        --focus <area>        — architecture, security, performance, testing, etc.
        --scope <path>        — path to review (file or directory)
        --questions <n>       — number of clarifying questions (0 to skip)
        --severity <level>    — low, medium, high, critical
        --max <n>             — max findings to report
        --no-examples         — skip code examples in output

    Args:
        args_str: Raw argument string after /recommend.

    Returns:
        RecommendConfig with parsed values.
    """
    config = RecommendConfig()

    if not args_str.strip():
        return config

    parts = args_str.strip().split()
    i = 0

    while i < len(parts):
        token = parts[i].lower()

        if token == "--persona" and i + 1 < len(parts):
            i += 1
            try:
                config.persona = Persona(parts[i].lower())
            except ValueError:
                logger.warning("Unknown persona: %s, using balanced", parts[i])
        elif token == "--focus" and i + 1 < len(parts):
            i += 1
            try:
                config.focus = Focus(parts[i].lower().replace("-", "_"))
            except ValueError:
                logger.warning("Unknown focus: %s, using general", parts[i])
        elif token == "--scope" and i + 1 < len(parts):
            i += 1
            config.scope_path = parts[i]
            # Auto-detect scope level
            if "/" not in parts[i] and "." in parts[i]:
                config.scope = Scope.FILE
            elif "/" in parts[i]:
                config.scope = Scope.DIRECTORY
            else:
                config.scope = Scope.PROJECT
        elif token == "--questions" and i + 1 < len(parts):
            i += 1
            try:
                config.num_questions = max(0, min(10, int(parts[i])))
            except ValueError:
                pass
        elif token == "--severity" and i + 1 < len(parts):
            i += 1
            config.severity_threshold = parts[i].lower()
        elif token == "--max" and i + 1 < len(parts):
            i += 1
            try:
                config.max_findings = max(1, min(100, int(parts[i])))
            except ValueError:
                pass
        elif token == "--no-examples":
            config.include_examples = False

        i += 1

    return config


def build_clarifying_prompt(config: RecommendConfig) -> str:
    """Build a prompt that asks clarifying questions before the review.

    Args:
        config: Recommendation configuration.

    Returns:
        Prompt string for the agent.
    """
    persona_desc = PERSONA_SYSTEM_PROMPTS.get(config.persona, PERSONA_SYSTEM_PROMPTS[Persona.BALANCED])

    questions_instruction = ""
    if config.num_questions > 0:
        questions_instruction = (
            f"\n\nBEFORE starting the review, you MUST ask exactly {config.num_questions} "
            "clarifying questions to better understand the user's goals and context. "
            "These questions should be specific and actionable — not generic. "
            "Ask about things like:\n"
            "- What are the most critical paths in this codebase?\n"
            "- Are there upcoming changes or deadlines affecting priorities?\n"
            "- What has caused the most bugs or friction recently?\n"
            "- Are there areas the team is least confident about?\n"
            "- What does the deployment/release process look like?\n\n"
            "Number each question. Wait for the user's answers before proceeding "
            "with the full review."
        )

    return (
        f"{persona_desc}\n\n"
        f"You are performing a review focused on: **{config.focus.value.replace('_', ' ')}**.\n\n"
        f"Scope: {config.scope.value} — `{config.scope_path}`\n"
        f"Severity threshold: {config.severity_threshold}\n"
        f"Max findings: {config.max_findings}\n"
        f"Include code examples: {'yes' if config.include_examples else 'no'}"
        f"{questions_instruction}"
    )


def build_review_prompt(config: RecommendConfig) -> str:
    """Build the full review prompt (used after clarifying questions are answered).

    Args:
        config: Recommendation configuration.

    Returns:
        Prompt string for the agent.
    """
    persona_desc = PERSONA_SYSTEM_PROMPTS.get(config.persona, PERSONA_SYSTEM_PROMPTS[Persona.BALANCED])
    focus_desc = FOCUS_INSTRUCTIONS.get(config.focus, FOCUS_INSTRUCTIONS[Focus.GENERAL])

    example_section = ""
    if config.include_examples:
        example_section = (
            "\n\nFor each finding, include a brief code example showing the current "
            "code and the recommended fix. Use diff format where appropriate."
        )

    return (
        f"{persona_desc}\n\n"
        f"## Review Task\n\n"
        f"{focus_desc}\n\n"
        f"**Scope:** Review `{config.scope_path}`\n"
        f"**Severity threshold:** Only report {config.severity_threshold} and above\n"
        f"**Max findings:** {config.max_findings}\n"
        f"{example_section}\n\n"
        "## Output Format\n\n"
        "Structure your review as:\n\n"
        "### Executive Summary\n"
        "2-3 sentences on overall health and most critical issues.\n\n"
        "### Findings\n"
        "For each finding:\n"
        "1. **[SEVERITY] Title** — one-line description\n"
        "2. **Location** — file:line\n"
        "3. **Issue** — what's wrong and why it matters\n"
        "4. **Recommendation** — how to fix it\n"
        "5. **Impact** — what improves if fixed\n\n"
        "### Recommendations\n"
        "Prioritized list of improvements, grouped by effort (quick wins vs. larger refactors).\n\n"
        "### Score\n"
        "Rate the reviewed area on a 1-10 scale for: reliability, maintainability, "
        "security, performance, and developer experience. "
        "Include a brief justification for each score."
    )


def format_recommend_help() -> str:
    """Format help text for the /recommend command.

    Returns:
        Help text string.
    """
    lines: list[str] = []
    lines.append("/recommend — AI-powered code review and recommendations")
    lines.append("")
    lines.append("Usage: /recommend [options]")
    lines.append("")
    lines.append("Options:")
    lines.append("  --persona <name>     Review persona (default: balanced)")
    lines.append("    architect          Senior architect — design, coupling, scalability")
    lines.append("    security           Security engineer — OWASP, auth, secrets")
    lines.append("    performance        Performance engineer — algorithms, I/O, caching")
    lines.append("    dx                 Developer experience — ergonomics, docs, onboarding")
    lines.append("    balanced           Staff engineer — comprehensive review")
    lines.append("")
    lines.append("  --focus <area>       Focus area (default: general)")
    lines.append("    architecture       Module boundaries, abstractions, coupling")
    lines.append("    security           Vulnerabilities, auth, secrets")
    lines.append("    performance        Bottlenecks, algorithms, caching")
    lines.append("    testing            Coverage, quality, edge cases")
    lines.append("    code-quality       Readability, consistency, dead code")
    lines.append("    documentation      Docstrings, README, API docs")
    lines.append("    dependencies       Outdated packages, licenses, CVEs")
    lines.append("    deployment         CI/CD, health checks, observability")
    lines.append("    general            Comprehensive review")
    lines.append("")
    lines.append("  --scope <path>       Path to review (default: full project)")
    lines.append("  --questions <n>      Clarifying questions before review (default: 3, 0 to skip)")
    lines.append("  --severity <level>   Minimum severity: low, medium, high, critical")
    lines.append("  --max <n>            Max findings to report (default: 25)")
    lines.append("  --no-examples        Skip code examples in output")
    lines.append("")
    lines.append("Examples:")
    lines.append("  /recommend")
    lines.append("  /recommend --persona architect --focus architecture")
    lines.append("  /recommend --focus security --scope libs/bog-agents")
    lines.append("  /recommend --persona dx --questions 5")
    lines.append("  /recommend --focus testing --questions 0")

    return "\n".join(lines)
