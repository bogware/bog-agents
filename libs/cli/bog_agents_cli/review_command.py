"""Code review workflow command.

Feature #14: /review command — structured code review workflow that
analyzes staged changes, specific files, or recent commits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReviewTarget:
    """What to review."""

    target_type: str
    """Type: 'staged', 'commit', 'files', 'branch', 'pr'."""

    value: str = ""
    """Target value (commit hash, file paths, branch name, PR number)."""

    files: list[str] = field(default_factory=list)
    """Specific files to review."""


@dataclass
class ReviewResult:
    """Result of a code review."""

    issues: list[dict[str, Any]] = field(default_factory=list)
    """List of issues found."""

    summary: str = ""
    """Overall review summary."""

    score: int = 0
    """Quality score (0-100)."""

    suggestions: list[str] = field(default_factory=list)
    """Improvement suggestions."""


def parse_review_args(args: str) -> ReviewTarget:
    """Parse /review command arguments.

    Supports:
    - `/review` — review staged changes
    - `/review HEAD~1` — review last commit
    - `/review file1.py file2.py` — review specific files
    - `/review --branch feature-x` — review branch diff

    Args:
        args: Command arguments string.

    Returns:
        ReviewTarget describing what to review.
    """
    args = args.strip()

    if not args:
        return ReviewTarget(target_type="staged")

    parts = args.split()

    if parts[0] == "--branch" and len(parts) > 1:
        return ReviewTarget(target_type="branch", value=parts[1])

    if parts[0] == "--pr" and len(parts) > 1:
        return ReviewTarget(target_type="pr", value=parts[1])

    # Check if it looks like a commit hash
    if len(parts) == 1 and (
        parts[0].startswith("HEAD")
        or (len(parts[0]) >= 7 and all(c in "0123456789abcdef" for c in parts[0]))
    ):
        return ReviewTarget(target_type="commit", value=parts[0])

    # Otherwise treat as file paths
    return ReviewTarget(target_type="files", files=parts)


def generate_review_prompt(target: ReviewTarget) -> str:
    """Generate a prompt for the AI to perform a code review.

    Args:
        target: What to review.

    Returns:
        Review prompt string for the agent.
    """
    lines = [
        "# Code Review Request\n",
        "Please perform a thorough code review with the following focus areas:\n",
        "1. **Correctness**: Logic errors, edge cases, off-by-one errors",
        "2. **Security**: Injection risks, auth issues, data exposure",
        "3. **Performance**: Unnecessary allocations, N+1 queries, blocking calls",
        "4. **Maintainability**: Code clarity, naming, documentation",
        "5. **Testing**: Missing test coverage, edge cases not tested\n",
    ]

    if target.target_type == "staged":
        lines.append("Review the currently staged git changes (`git diff --cached`).")
    elif target.target_type == "commit":
        lines.append(
            f"Review the changes in commit `{target.value}` (`git show {target.value}`)."
        )
    elif target.target_type == "files":
        files_str = ", ".join(f"`{f}`" for f in target.files)
        lines.append(f"Review these files: {files_str}")
    elif target.target_type == "branch":
        lines.append(f"Review all changes on branch `{target.value}` compared to main.")
    elif target.target_type == "pr":
        lines.append(f"Review pull request #{target.value}.")

    lines.extend(
        [
            "\n## Output Format\n",
            "For each issue found, provide:",
            "- **File and line**: Where the issue is",
            "- **Severity**: critical / warning / suggestion",
            "- **Description**: What the issue is and why it matters",
            "- **Fix**: Suggested fix or approach\n",
            "End with an overall summary and quality score (0-100).",
        ]
    )

    return "\n".join(lines)
