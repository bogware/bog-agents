"""Middleware providing git-native workflow tools.

Feature #15: Git-native workflows — git tools for the agent.
Feature #43: Git-aware edit_file — auto-stage changes, show diffs inline.

W4 note (audit pass): the actual tool definitions now live in
:mod:`bog_agents.tools.bundles` as the canonical ``git_tools_bundle``
factory. This middleware class is a thin shim that delegates to the
bundle so existing callers (``f.enable_git_tools=True`` plumbing in
``graph.py``, downstream user middleware lists) keep working. New code
that only wants the git tools should prefer
``create_agent(tools=[*git_tools_bundle(working_dir)])`` and skip the
middleware indirection entirely.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain_core.tools import BaseTool
from typing_extensions import TypedDict

from bog_agents.git_env import hardened_git_env

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
            env=hardened_git_env(),
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
        """Build git tools by delegating to :func:`git_tools_bundle`.

        Kept thin on purpose — the actual tool definitions live in
        ``bog_agents.tools.bundles`` so callers who don't want a
        middleware can ``import git_tools_bundle`` directly and skip
        the wrap stack entirely. See module docstring.
        """
        from bog_agents.tools.bundles import git_tools_bundle

        return git_tools_bundle(self._working_dir, auto_stage=self._auto_stage)
