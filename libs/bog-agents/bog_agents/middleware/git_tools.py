"""Middleware providing git-native workflow tools.

Feature #15: Git-native workflows — git tools for the agent.
Feature #43: Git-aware edit_file — auto-stage changes, show diffs inline.
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


class GitToolsState(TypedDict):
    """State for the git tools middleware."""


def _run_git(working_dir: Path, *args: str, timeout: int = 30) -> str:
    """Run a git command and return output.

    Args:
        working_dir: Working directory for the git command.
        *args: Git command arguments.
        timeout: Command timeout in seconds.

    Returns:
        Combined stdout/stderr output.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = result.stdout
        if result.returncode != 0:
            output = f"[exit code {result.returncode}]\n{result.stderr or result.stdout}"
        return output.strip()
    except subprocess.TimeoutExpired:
        return f"Error: git command timed out after {timeout}s"
    except FileNotFoundError:
        return "Error: git is not installed or not in PATH"


class GitToolsMiddleware(AgentMiddleware[GitToolsState, ContextT, ResponseT]):
    """Middleware providing git workflow tools to the agent.

    Adds tools for common git operations: status, diff, log, commit,
    branch management, stash, and blame.

    Args:
        working_dir: Repository root directory.
        auto_stage: Whether to automatically stage changes after edits.
    """

    state_schema = GitToolsState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        auto_stage: bool = False,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._auto_stage = auto_stage
        self.tools = self._build_tools()

    def _git(self, *args: str, timeout: int = 30) -> str:
        """Run a git command.

        Args:
            *args: Git arguments.
            timeout: Command timeout.

        Returns:
            Command output.
        """
        return _run_git(self._working_dir, *args, timeout=timeout)

    def _build_tools(self) -> list[BaseTool]:
        """Build git tools."""
        middleware = self

        def git_status(
            runtime: ToolRuntime[None, GitToolsState],
        ) -> str:
            """Show the working tree status including staged, unstaged, and untracked files."""
            return middleware._git("status", "--short", "--branch")

        def git_diff(
            runtime: ToolRuntime[None, GitToolsState],
            staged: bool = False,
            path: str | None = None,
        ) -> str:
            """Show changes in the working directory. Use staged=True for staged changes only."""
            args = ["diff"]
            if staged:
                args.append("--cached")
            if path:
                args.extend(["--", path])
            return middleware._git(*args)

        def git_log(
            runtime: ToolRuntime[None, GitToolsState],
            count: int = 10,
            oneline: bool = True,
        ) -> str:
            """Show recent commit history. Default 10 commits in oneline format."""
            args = ["log", f"-{count}"]
            if oneline:
                args.append("--oneline")
            return middleware._git(*args)

        def git_commit(
            runtime: ToolRuntime[None, GitToolsState],
            message: Annotated[str, "Commit message following Conventional Commits format"],
            files: Annotated[list[str] | None, "Specific files to stage and commit. If None, commits all staged changes."] = None,
        ) -> str:
            """Create a git commit. Optionally specify files to stage first."""
            if files:
                for f in files:
                    middleware._git("add", f)
            return middleware._git("commit", "-m", message)

        def git_add(
            runtime: ToolRuntime[None, GitToolsState],
            paths: Annotated[list[str], "File paths to stage"],
        ) -> str:
            """Stage files for commit."""
            results = []
            for path in paths:
                result = middleware._git("add", path)
                if result:
                    results.append(result)
            return "\n".join(results) if results else f"Staged {len(paths)} file(s)"

        def git_branch(
            runtime: ToolRuntime[None, GitToolsState],
            name: str | None = None,
            checkout: bool = False,
        ) -> str:
            """List branches, create a new branch, or checkout an existing one."""
            if name is not None:
                # P1-9: refuse ref names that would be re-interpreted as
                # flags by git. Defensive check that catches the
                # hostile-model-supplies-flag-shaped-branch-name class
                # of attack. See worktree.py _validate_git_ref.
                from bog_agents.middleware.worktree import _validate_git_ref

                try:
                    name = _validate_git_ref(name, label="branch")
                except ValueError as exc:
                    return f"Error: {exc}"
            if name and checkout:
                # ``--`` after the option-bearing arg makes ``name`` a
                # positional that git's parser can't reinterpret.
                return middleware._git("checkout", "-b", name, "--")
            if name:
                return middleware._git("branch", "--", name)
            if checkout:
                return middleware._git("branch", "-a")
            return middleware._git("branch", "-a", "--sort=-committerdate")

        def git_stash(
            runtime: ToolRuntime[None, GitToolsState],
            action: str = "list",
            message: str | None = None,
        ) -> str:
            """Manage git stash. action: 'push', 'pop', 'list', 'show', 'drop'."""
            if action == "push":
                args = ["stash", "push"]
                if message:
                    args.extend(["-m", message])
                return middleware._git(*args)
            if action == "pop":
                return middleware._git("stash", "pop")
            if action == "show":
                return middleware._git("stash", "show", "-p")
            if action == "drop":
                return middleware._git("stash", "drop")
            return middleware._git("stash", "list")

        def git_blame(
            runtime: ToolRuntime[None, GitToolsState],
            path: Annotated[str, "File path to blame"],
            start_line: int | None = None,
            end_line: int | None = None,
        ) -> str:
            """Show who last modified each line of a file."""
            args = ["blame", "--no-pager"]
            if start_line and end_line:
                args.extend([f"-L{start_line},{end_line}"])
            args.append(path)
            return middleware._git(*args)

        def git_show(
            runtime: ToolRuntime[None, GitToolsState],
            ref: str = "HEAD",
        ) -> str:
            """Show details of a commit. Default shows the latest commit."""
            return middleware._git("show", "--stat", ref)

        return [
            StructuredTool.from_function(name="git_status", description="Show working tree status.", func=git_status),
            StructuredTool.from_function(name="git_diff", description="Show file changes. staged=True for staged only.", func=git_diff),
            StructuredTool.from_function(name="git_log", description="Show commit history.", func=git_log),
            StructuredTool.from_function(name="git_commit", description="Create a git commit.", func=git_commit),
            StructuredTool.from_function(name="git_add", description="Stage files for commit.", func=git_add),
            StructuredTool.from_function(name="git_branch", description="Manage branches.", func=git_branch),
            StructuredTool.from_function(name="git_stash", description="Manage stash.", func=git_stash),
            StructuredTool.from_function(name="git_blame", description="Show line-by-line authorship.", func=git_blame),
            StructuredTool.from_function(name="git_show", description="Show commit details.", func=git_show),
        ]
