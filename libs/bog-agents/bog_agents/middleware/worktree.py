"""Middleware providing git worktree isolation for parallel agent execution.

Feature #1: Git worktree isolation — each agent session gets its own worktree.
Feature #2: Multi-agent orchestrator — spawn N agents in parallel worktrees.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
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


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    path: Path
    branch: str
    commit: str | None = None
    is_main: bool = False
    agent_id: str | None = None


class WorktreeState(TypedDict):
    """State for the worktree middleware."""


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


def create_worktree(
    repo_dir: Path,
    branch: str,
    *,
    base_dir: Path | None = None,
) -> WorktreeInfo:
    """Create a new git worktree.

    Args:
        repo_dir: Root repository directory.
        branch: Branch name for the worktree.
        base_dir: Base directory for worktrees. Defaults to temp dir.

    Returns:
        WorktreeInfo with path and branch details.
    """
    if base_dir is None:
        base_dir = Path(tempfile.mkdtemp(prefix="bog-agents-worktree-"))

    worktree_path = base_dir / branch.replace("/", "-")
    result = _run_git(repo_dir, "worktree", "add", "-b", branch, str(worktree_path))
    if result.startswith("[exit code"):
        # Branch might already exist, try without -b
        result = _run_git(repo_dir, "worktree", "add", str(worktree_path), branch)

    commit = _run_git(worktree_path, "rev-parse", "HEAD") if worktree_path.exists() else None
    return WorktreeInfo(path=worktree_path, branch=branch, commit=commit)


def remove_worktree(repo_dir: Path, worktree_path: Path) -> str:
    """Remove a git worktree.

    Args:
        repo_dir: Root repository directory.
        worktree_path: Path to the worktree to remove.

    Returns:
        Result message.
    """
    return _run_git(repo_dir, "worktree", "remove", str(worktree_path), "--force")


def list_worktrees(repo_dir: Path) -> list[WorktreeInfo]:
    """List all git worktrees.

    Args:
        repo_dir: Root repository directory.

    Returns:
        List of WorktreeInfo objects.
    """
    output = _run_git(repo_dir, "worktree", "list", "--porcelain")
    worktrees: list[WorktreeInfo] = []
    current: dict[str, str] = {}

    for line in output.split("\n"):
        if line.startswith("worktree "):
            if current:
                worktrees.append(
                    WorktreeInfo(
                        path=Path(current.get("worktree", "")),
                        branch=current.get("branch", "").replace("refs/heads/", ""),
                        commit=current.get("HEAD"),
                        is_main=current.get("worktree", "") == str(repo_dir),
                    )
                )
            current = {"worktree": line.split(" ", 1)[1]}
        elif line.startswith("HEAD "):
            current["HEAD"] = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1]

    if current:
        worktrees.append(
            WorktreeInfo(
                path=Path(current.get("worktree", "")),
                branch=current.get("branch", "").replace("refs/heads/", ""),
                commit=current.get("HEAD"),
                is_main=current.get("worktree", "") == str(repo_dir),
            )
        )
    return worktrees


@dataclass
class AgentThread:
    """A tracked agent thread with its worktree."""

    thread_id: str
    label: str
    worktree: WorktreeInfo | None = None
    status: str = "running"
    task: str = ""


class WorktreeMiddleware(AgentMiddleware[WorktreeState, ContextT, ResponseT]):
    """Middleware providing git worktree isolation for parallel agents.

    Creates isolated worktrees so multiple agents can work on the same
    repository without file conflicts.

    Args:
        working_dir: Repository root directory.
    """

    state_schema = WorktreeState

    def __init__(self, *, working_dir: Path | None = None) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._threads: dict[str, AgentThread] = {}
        self.tools = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build worktree management tools."""
        middleware = self

        def create_agent_worktree(
            runtime: ToolRuntime[None, WorktreeState],
            branch_name: Annotated[str, "Branch name for the new worktree"],
            label: Annotated[str, "Human-readable label for this agent thread"] = "",
        ) -> str:
            """Create a new git worktree for isolated agent work."""
            wt = create_worktree(middleware._working_dir, branch_name)
            thread_id = branch_name.replace("/", "-")
            middleware._threads[thread_id] = AgentThread(
                thread_id=thread_id,
                label=label or branch_name,
                worktree=wt,
            )
            return f"Created worktree at {wt.path} on branch {wt.branch}"

        def list_agent_worktrees(
            runtime: ToolRuntime[None, WorktreeState],
        ) -> str:
            """List all active git worktrees."""
            worktrees = list_worktrees(middleware._working_dir)
            if not worktrees:
                return "No worktrees found."
            lines = []
            for wt in worktrees:
                marker = " (main)" if wt.is_main else ""
                lines.append(f"  {wt.branch}{marker}: {wt.path}")
            return "Active worktrees:\n" + "\n".join(lines)

        def remove_agent_worktree(
            runtime: ToolRuntime[None, WorktreeState],
            branch_name: Annotated[str, "Branch name of the worktree to remove"],
        ) -> str:
            """Remove a git worktree and clean up."""
            thread_id = branch_name.replace("/", "-")
            thread = middleware._threads.pop(thread_id, None)
            if thread and thread.worktree:
                result = remove_worktree(middleware._working_dir, thread.worktree.path)
                return f"Removed worktree: {result}"
            # Try removing by finding the worktree
            worktrees = list_worktrees(middleware._working_dir)
            for wt in worktrees:
                if wt.branch == branch_name and not wt.is_main:
                    return remove_worktree(middleware._working_dir, wt.path)
            return f"Worktree for branch '{branch_name}' not found."

        def merge_worktree(
            runtime: ToolRuntime[None, WorktreeState],
            source_branch: Annotated[str, "Branch to merge from"],
            target_branch: Annotated[str, "Branch to merge into"] = "main",
        ) -> str:
            """Merge changes from one worktree branch into another."""
            result = _run_git(middleware._working_dir, "checkout", target_branch)
            if result.startswith("[exit code"):
                return f"Failed to checkout {target_branch}: {result}"
            merge_result = _run_git(middleware._working_dir, "merge", source_branch)
            return f"Merge result: {merge_result}"

        return [
            StructuredTool.from_function(
                name="create_worktree",
                description="Create a new git worktree for isolated parallel work.",
                func=create_agent_worktree,
            ),
            StructuredTool.from_function(
                name="list_worktrees",
                description="List all active git worktrees.",
                func=list_agent_worktrees,
            ),
            StructuredTool.from_function(
                name="remove_worktree",
                description="Remove a git worktree.",
                func=remove_agent_worktree,
            ),
            StructuredTool.from_function(
                name="merge_worktree",
                description="Merge changes from one worktree branch into another.",
                func=merge_worktree,
            ),
        ]
