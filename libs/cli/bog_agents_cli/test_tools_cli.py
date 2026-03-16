"""Test generation and quality tools CLI interface.

Feature #35: Auto test generation.
Feature #36: Test coverage analysis.
Feature #37: Mutation testing.
Feature #38: Benchmark runner.
Feature #40: Dependency audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CoverageReport:
    """Test coverage report summary."""

    total_statements: int = 0
    covered_statements: int = 0
    missing_lines: list[str] = field(default_factory=list)
    coverage_percent: float = 0.0
    files: dict[str, float] = field(default_factory=dict)


def parse_test_command(text: str) -> dict[str, str]:
    """Parse a /test command.

    Subcommands:
    - /test generate <file> — generate tests for a file
    - /test coverage [path] — run with coverage
    - /test gaps <file> — find untested code
    - /test benchmark [path] — run benchmarks
    - /test audit — audit dependencies

    Args:
        text: Command text after /test.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else "coverage"
    arg = parts[1] if len(parts) > 1 else ""
    return {"action": action, "argument": arg}


def generate_test_prompt(source_file: str, framework: str = "pytest") -> str:
    """Generate a prompt for test generation.

    Args:
        source_file: Source file to test.
        framework: Test framework name.

    Returns:
        Prompt for the agent.
    """
    return (
        f"Generate comprehensive unit tests for {source_file} using {framework}.\n"
        f"Follow these guidelines:\n"
        f"1. Test all public functions and methods\n"
        f"2. Include edge cases and error scenarios\n"
        f"3. Use descriptive test names\n"
        f"4. Mock external dependencies\n"
        f"5. Follow the existing test patterns in the project\n"
    )


def generate_coverage_prompt(path: str = "tests/") -> str:
    """Generate a prompt for coverage analysis.

    Args:
        path: Path to test directory.

    Returns:
        Prompt for the agent.
    """
    return (
        f"Run tests at {path} with coverage and analyze the results.\n"
        f"Report:\n"
        f"1. Overall coverage percentage\n"
        f"2. Files with lowest coverage\n"
        f"3. Specific uncovered lines/functions\n"
        f"4. Suggestions for improving coverage\n"
    )


def generate_audit_prompt() -> str:
    """Generate a prompt for dependency audit.

    Returns:
        Prompt for the agent.
    """
    return (
        "Audit project dependencies for:\n"
        "1. Known security vulnerabilities\n"
        "2. Outdated packages with available updates\n"
        "3. Unused dependencies\n"
        "4. License compatibility issues\n"
        "Report findings with recommended actions.\n"
    )
