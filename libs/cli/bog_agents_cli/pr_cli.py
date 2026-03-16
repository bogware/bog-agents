"""Pull request management CLI interface.

Feature #28: PR creation & management.
Feature #29: Inline diff comments.
Feature #30: Auto-PR description.
Feature #31: Merge conflict resolution.
Feature #32: Blame-aware editing.
Feature #33: Commit splitting.
Feature #34: Interactive rebase helper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PRInfo:
    """Pull request information."""

    number: int
    title: str
    body: str = ""
    base_branch: str = "main"
    head_branch: str = ""
    draft: bool = False
    labels: list[str] = field(default_factory=list)


def parse_pr_command(text: str) -> dict[str, str]:
    """Parse a /pr command.

    Subcommands:
    - /pr create [title] — create a PR
    - /pr list — list open PRs
    - /pr review <number> — show PR review comments
    - /pr describe — auto-generate PR description
    - /pr conflicts — show and help resolve conflicts

    Args:
        text: Command text after /pr.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else "list"
    arg = parts[1] if len(parts) > 1 else ""
    return {"action": action, "argument": arg}


def generate_pr_prompt(info: PRInfo) -> str:
    """Generate a prompt for creating a PR.

    Args:
        info: PR configuration.

    Returns:
        Prompt for the agent.
    """
    prompt = "Create a pull request with the following details:\n"
    prompt += f"  Title: {info.title}\n"
    prompt += f"  Base: {info.base_branch}\n"
    if info.head_branch:
        prompt += f"  Head: {info.head_branch}\n"
    if info.draft:
        prompt += "  Draft: yes\n"
    if info.body:
        prompt += f"\nDescription:\n{info.body}\n"
    else:
        prompt += "\nAuto-generate a description from the commit history.\n"
    return prompt


def generate_conflict_resolution_prompt() -> str:
    """Generate a prompt for conflict resolution.

    Returns:
        Prompt for the agent.
    """
    return (
        "Analyze the current merge conflicts and help resolve them.\n"
        "For each conflicted file:\n"
        "1. Show both sides of the conflict\n"
        "2. Explain what each side is trying to do\n"
        "3. Suggest the best resolution\n"
        "4. Apply the resolution if approved\n"
    )


def generate_bisect_prompt(bad_ref: str, good_ref: str, test_command: str = "") -> str:
    """Generate a prompt for git bisect.

    Args:
        bad_ref: Known bad commit.
        good_ref: Known good commit.
        test_command: Optional test command to verify.

    Returns:
        Prompt for the agent.
    """
    prompt = "Start a git bisect to find when the bug was introduced.\n"
    prompt += f"  Bad: {bad_ref}\n"
    prompt += f"  Good: {good_ref}\n"
    if test_command:
        prompt += f"  Test with: {test_command}\n"
    return prompt
