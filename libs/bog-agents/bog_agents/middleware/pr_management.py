"""Middleware for pull request and advanced git operations.

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
import subprocess
from pathlib import Path
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


def _run_cmd(working_dir: Path, *args: str, timeout: int = 30) -> str:
    """Run a shell command and return output.

    Args:
        working_dir: Working directory.
        *args: Command and arguments.
        timeout: Command timeout.

    Returns:
        Command output.
    """
    try:
        result = subprocess.run(  # noqa: S603
            list(args),
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return f"[exit code {result.returncode}]\n{result.stderr or result.stdout}".strip()
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"Error: {e}"


class PRManagementState(TypedDict):
    """State for PR management middleware."""


class PRManagementMiddleware(AgentMiddleware[PRManagementState, ContextT, ResponseT]):
    """Middleware for pull request creation and advanced git operations.

    Provides tools for creating PRs, resolving conflicts, and managing
    complex git workflows.

    Args:
        working_dir: Repository root directory.
    """

    state_schema = PRManagementState

    def __init__(self, *, working_dir: Path | None = None) -> None:
        self._working_dir = working_dir or Path.cwd()
        self.tools = self._build_tools()

    def _git(self, *args: str, timeout: int = 30) -> str:
        """Run a git command."""
        return _run_cmd(self._working_dir, "git", *args, timeout=timeout)

    def _gh(self, *args: str, timeout: int = 60) -> str:
        """Run a gh (GitHub CLI) command."""
        return _run_cmd(self._working_dir, "gh", *args, timeout=timeout)

    def _build_tools(self) -> list[BaseTool]:
        """Build PR management tools."""
        middleware = self

        def create_pull_request(
            runtime: ToolRuntime[None, PRManagementState],
            title: Annotated[str, "PR title (keep under 70 chars)"],
            body: Annotated[str, "PR description with summary and test plan"],
            base: Annotated[str, "Base branch"] = "main",
            draft: Annotated[bool, "Create as draft PR"] = False,
        ) -> str:
            """Create a GitHub pull request from the current branch."""
            args = ["pr", "create", "--title", title, "--body", body, "--base", base]
            if draft:
                args.append("--draft")
            return middleware._gh(*args, timeout=60)

        def list_pull_requests(
            runtime: ToolRuntime[None, PRManagementState],
            state: Annotated[str, "Filter: 'open', 'closed', 'merged', 'all'"] = "open",
        ) -> str:
            """List pull requests in the repository."""
            return middleware._gh("pr", "list", "--state", state)

        def pr_review_comments(
            runtime: ToolRuntime[None, PRManagementState],
            pr_number: Annotated[int, "Pull request number"],
        ) -> str:
            """List review comments on a pull request."""
            return middleware._gh("pr", "view", str(pr_number), "--comments")

        def add_pr_comment(
            runtime: ToolRuntime[None, PRManagementState],
            pr_number: Annotated[int, "Pull request number"],
            comment: Annotated[str, "Comment text"],
        ) -> str:
            """Add a comment to a pull request."""
            return middleware._gh("pr", "comment", str(pr_number), "--body", comment)

        def auto_pr_description(
            runtime: ToolRuntime[None, PRManagementState],
            base_branch: Annotated[str, "Base branch to compare against"] = "main",
        ) -> str:
            """Generate a PR description from commit history and changes."""
            log = middleware._git("log", f"{base_branch}..HEAD", "--oneline")
            diff_stat = middleware._git("diff", f"{base_branch}..HEAD", "--stat")
            branch = middleware._git("branch", "--show-current")

            return (
                f"## Auto-Generated PR Description\n\n"
                f"**Branch:** {branch}\n"
                f"**Base:** {base_branch}\n\n"
                f"### Commits\n```\n{log}\n```\n\n"
                f"### Changes\n```\n{diff_stat}\n```\n\n"
                f"### Summary\n"
                f"[Edit this section to describe the purpose of these changes]\n\n"
                f"### Test Plan\n"
                f"- [ ] Unit tests pass\n"
                f"- [ ] Integration tests pass\n"
                f"- [ ] Manual testing completed\n"
            )

        def resolve_conflicts(
            runtime: ToolRuntime[None, PRManagementState],
            strategy: Annotated[str, "Resolution strategy: 'ours', 'theirs', 'manual'"] = "manual",
        ) -> str:
            """Help resolve merge conflicts."""
            status = middleware._git("status", "--short")
            conflicted = [line for line in status.split("\n") if line.startswith("UU") or line.startswith("AA")]

            if not conflicted:
                return "No merge conflicts detected."

            lines = [f"Found {len(conflicted)} conflicted files:"]
            for cf in conflicted:
                filepath = cf[3:].strip()
                lines.append(f"  {filepath}")

                if strategy == "ours":
                    middleware._git("checkout", "--ours", filepath)
                    middleware._git("add", filepath)
                    lines.append("    → Resolved using 'ours'")
                elif strategy == "theirs":
                    middleware._git("checkout", "--theirs", filepath)
                    middleware._git("add", filepath)
                    lines.append("    → Resolved using 'theirs'")
                else:
                    # Show conflict markers for manual resolution
                    try:
                        content = (middleware._working_dir / filepath).read_text(encoding="utf-8", errors="replace")
                        conflict_sections = []
                        in_conflict = False
                        for line in content.split("\n"):
                            if line.startswith("<<<<<<<"):
                                in_conflict = True
                            if in_conflict:
                                conflict_sections.append(line)
                            if line.startswith(">>>>>>>"):
                                in_conflict = False
                        if conflict_sections:
                            lines.append("    Conflicts:\n    " + "\n    ".join(conflict_sections[:20]))
                    except OSError:
                        pass

            return "\n".join(lines)

        def git_bisect_start(
            runtime: ToolRuntime[None, PRManagementState],
            bad_ref: Annotated[str, "Known bad commit (e.g., 'HEAD')"] = "HEAD",
            good_ref: Annotated[str, "Known good commit (e.g., a commit hash)"] = "",
        ) -> str:
            """Start a git bisect to find when a bug was introduced."""
            if not good_ref:
                return "Error: Must provide a known good commit reference."
            result = middleware._git("bisect", "start")
            result += "\n" + middleware._git("bisect", "bad", bad_ref)
            result += "\n" + middleware._git("bisect", "good", good_ref)
            return f"Bisect started:\n{result}"

        def git_bisect_step(
            runtime: ToolRuntime[None, PRManagementState],
            verdict: Annotated[str, "'good' or 'bad' for the current commit"],
        ) -> str:
            """Mark the current bisect commit as good or bad."""
            return middleware._git("bisect", verdict)

        def git_bisect_reset(
            runtime: ToolRuntime[None, PRManagementState],
        ) -> str:
            """End the bisect session and return to the original branch."""
            return middleware._git("bisect", "reset")

        return [
            StructuredTool.from_function(name="create_pr", description="Create a GitHub pull request.", func=create_pull_request),
            StructuredTool.from_function(name="list_prs", description="List pull requests.", func=list_pull_requests),
            StructuredTool.from_function(name="pr_comments", description="List PR review comments.", func=pr_review_comments),
            StructuredTool.from_function(name="add_pr_comment", description="Add a comment to a PR.", func=add_pr_comment),
            StructuredTool.from_function(name="auto_pr_description", description="Generate PR description.", func=auto_pr_description),
            StructuredTool.from_function(name="resolve_conflicts", description="Resolve merge conflicts.", func=resolve_conflicts),
            StructuredTool.from_function(name="git_bisect_start", description="Start git bisect.", func=git_bisect_start),
            StructuredTool.from_function(name="git_bisect_step", description="Mark bisect commit.", func=git_bisect_step),
            StructuredTool.from_function(name="git_bisect_reset", description="End bisect session.", func=git_bisect_reset),
        ]
