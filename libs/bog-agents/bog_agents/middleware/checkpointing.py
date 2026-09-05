"""Middleware for automatic git checkpointing before file changes.

Feature #3: Creates git snapshots before any file modification so changes
can be rolled back with /undo or /rewind commands.

Feature #5: Provides /diff and /undo capabilities by tracking change history.

Feature #39: Undo/rewind with conversation vs code split.

Feature #43: Git-aware edit_file — auto-stage changes, show diffs inline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.git_env import NO_EXTERNAL_DIFF, hardened_git_env

logger = logging.getLogger(__name__)

# Tools that modify files and should trigger checkpointing
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "multi_edit_file", "execute"})

# Committer identity for shadow checkpoint commits, passed per-invocation so
# checkpointing does not depend on (or mutate) the user's global git config.
_CHECKPOINT_IDENTITY_ARGS = (
    "-c",
    "user.name=bog-agents",
    "-c",
    "user.email=bog-agents@localhost",
    "-c",
    "commit.gpgsign=false",
)


__all__ = [
    "Checkpoint",
    "CheckpointState",
    "CheckpointingMiddleware",
]


@dataclass
class Checkpoint:
    """A single checkpoint representing a point-in-time snapshot."""

    commit_hash: str
    """The shadow git commit hash for this checkpoint."""

    message: str
    """Human-readable description of what triggered this checkpoint."""

    tool_call_id: str
    """The tool call ID that triggered this checkpoint."""

    files_changed: list[str] = field(default_factory=list)
    """List of file paths that were modified after this checkpoint."""


class CheckpointState(TypedDict):
    """State for the checkpointing middleware."""


class CheckpointingMiddleware(AgentMiddleware[CheckpointState, ContextT, ResponseT]):
    """Middleware that creates git checkpoints before file modifications.

    Maintains a shadow git repository to track all agent-made changes,
    enabling rollback and diff capabilities without polluting the user's
    actual git history.

    Args:
        working_dir: The working directory to checkpoint.
        enabled: Whether checkpointing is active.
        shadow_dir: Optional custom directory for shadow git repo.
    """

    state_schema = CheckpointState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        enabled: bool = True,
        shadow_dir: Path | None = None,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._enabled = enabled
        self._shadow_dir = shadow_dir
        self._checkpoints: list[Checkpoint] = []
        self._initialized = False
        # Set once when git is unavailable (not on PATH) or a git call times
        # out, so the self-disable warning is logged exactly once rather than
        # on every mutating tool call.
        self._git_unavailable = False
        # Checkpointing stages files (`git add -A`) to build stash snapshots. It
        # MUST NOT touch the user's real git index — a user running the agent in
        # their own repo would otherwise find their whole working tree staged.
        # All checkpoint git commands operate against an isolated, throwaway index
        # via GIT_INDEX_FILE, so the user's index is never modified.
        self._index_file = str(Path(tempfile.mkdtemp(prefix="bog-agents-ckpt-")) / "index")

    def _run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run a git command against an isolated index in the working directory.

        Args:
            *args: Git command arguments.
            cwd: Override working directory.

        Returns:
            Completed process result. On a missing `git` binary (not on PATH)
            or a git call that exceeds the timeout, checkpointing self-disables
            and a synthetic non-zero result is returned so every caller degrades
            gracefully instead of the exception propagating out of the tool node
            and aborting the whole agent turn.
        """
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd or self._working_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=hardened_git_env({**os.environ, "GIT_INDEX_FILE": self._index_file}),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # FileNotFoundError (git not installed) or TimeoutExpired (git add -A
            # on a huge/cold repo exceeding the deadline). langgraph's default
            # tool-error handler re-raises anything that is not a
            # ToolInvocationError, so an uncaught error here kills the run — and
            # the CLI ships checkpointing on by default, making every write_file
            # / edit_file / execute crash on a git-less box. Degrade instead.
            self._enabled = False
            if not self._git_unavailable:
                self._git_unavailable = True
                logger.warning(
                    "Checkpointing disabled: `git %s` failed (%s: %s). Undo/rewind will be unavailable this session.",
                    " ".join(args[:1]),
                    type(exc).__name__,
                    str(exc)[:200],
                )
            return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr=str(exc))

    def _ensure_initialized(self) -> None:
        """Initialize the shadow git tracking if not already done.

        Logs warnings on git failures so an operator can tell when
        checkpointing has silently disabled itself (disk full,
        permission denied, missing git). Previously these failures were
        swallowed and users believed undo/rewind worked when it didn't.
        """
        if self._initialized or not self._enabled:
            return

        # Check if working dir is a git repo. A non-zero exit here is
        # the normal "not a repo yet" case — followed by ``git init``,
        # which IS load-bearing and should be loud on failure.
        result = self._run_git("rev-parse", "--is-inside-work-tree")
        if result.returncode != 0:
            init_result = self._run_git("init")
            if init_result.returncode != 0:
                logger.warning(
                    "Checkpointing disabled: `git init` failed in %s (exit %d). stderr: %s",
                    self._working_dir,
                    init_result.returncode,
                    (init_result.stderr or "").strip()[:200],
                )
                self._enabled = False
                self._initialized = True
                return

        # `git stash create` (used to record checkpoints) requires an existing
        # HEAD. On a repo with an UNBORN head (freshly `git init`ed, no commits)
        # it fails with "you do not have the initial commit yet", which silently
        # disabled checkpointing on modern git. Create a single initial commit in
        # that case ONLY. A repo that already has history (the common case — a
        # user running the agent in their real project) is left completely
        # untouched: we never `git add`/`git commit` into an existing history,
        # we just anchor the baseline at the user's current HEAD.
        if self._run_git("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
            self._run_git("add", "-A")
            commit_result = self._run_git(*_CHECKPOINT_IDENTITY_ARGS, "commit", "--allow-empty", "-q", "-m", "bog-agents-checkpoint-baseline")
            if commit_result.returncode != 0:
                logger.warning(
                    "Checkpointing initial commit failed: exit %d. stderr: %s",
                    commit_result.returncode,
                    (commit_result.stderr or "").strip()[:200],
                )

        head = self._run_git("rev-parse", "HEAD")
        if head.returncode == 0 and head.stdout.strip():
            self._checkpoints.append(
                Checkpoint(
                    commit_hash=head.stdout.strip(),
                    message="Initial checkpoint before agent modifications",
                    tool_call_id="init",
                )
            )
        self._initialized = True

    def _create_checkpoint(self, tool_name: str, tool_call_id: str) -> str | None:
        """Create a git checkpoint of the current state.

        Args:
            tool_name: Name of the tool about to modify files.
            tool_call_id: The tool call ID.

        Returns:
            The stash/commit hash, or None if checkpoint failed.
        """
        if not self._enabled:
            return None

        self._ensure_initialized()

        # Stage all current changes
        self._run_git("add", "-A")

        # Create a checkpoint commit on a detached ref. `git stash create` only
        # emits a hash when there are uncommitted changes; when the tree is clean
        # the state before this tool is just HEAD, so fall back to recording HEAD
        # (a checkpoint before every mutating tool must always exist so rewind can
        # restore to it).
        msg = f"bog-agents-checkpoint: before {tool_name} ({tool_call_id})"
        result = self._run_git("stash", "create", msg)

        checkpoint_hash = result.stdout.strip() if result.returncode == 0 else ""
        if not checkpoint_hash:
            head = self._run_git("rev-parse", "HEAD")
            checkpoint_hash = head.stdout.strip() if head.returncode == 0 else ""

        if checkpoint_hash:
            self._checkpoints.append(
                Checkpoint(
                    commit_hash=checkpoint_hash,
                    message=msg,
                    tool_call_id=tool_call_id,
                )
            )
            return checkpoint_hash
        return None

    def get_diff_since_last_checkpoint(self) -> str:
        """Get the diff of all changes since the last checkpoint.

        Returns:
            Git diff output as a string.
        """
        if not self._checkpoints:
            result = self._run_git("diff", *NO_EXTERNAL_DIFF)
            return result.stdout if result.returncode == 0 else "No changes detected."

        last = self._checkpoints[-1]
        result = self._run_git("diff", *NO_EXTERNAL_DIFF, last.commit_hash)
        return result.stdout if result.returncode == 0 else "Could not generate diff."

    def get_full_diff(self) -> str:
        """Get the diff of all agent changes from the initial checkpoint.

        Returns:
            Git diff output as a string.
        """
        if not self._checkpoints:
            return "No checkpoints available."

        first = self._checkpoints[0]
        result = self._run_git("diff", *NO_EXTERNAL_DIFF, first.commit_hash)
        return result.stdout if result.returncode == 0 else "Could not generate diff."

    def undo_last_change(self) -> str:
        """Revert to the last checkpoint.

        Returns:
            Status message describing what was undone.
        """
        if not self._checkpoints:
            return "No checkpoints available to undo."

        last = self._checkpoints[-1]

        # Restore the stashed state
        result = self._run_git("checkout", "--", ".")
        if result.returncode != 0:
            return f"Failed to undo: {result.stderr}"

        # Also try to apply the stash
        result = self._run_git("stash", "apply", last.commit_hash)
        if result.returncode == 0:
            self._checkpoints.pop()
            return f"Reverted to checkpoint: {last.message}"

        return f"Partial revert to checkpoint: {last.message}. Some conflicts may exist."

    def get_tools(self) -> list[BaseTool]:
        """Get the checkpoint-related tools.

        Returns:
            List of tools for diff, undo, and checkpoint management.
        """
        middleware = self

        def diff_tool(
            runtime: ToolRuntime[None, CheckpointState],
            scope: str = "last",
        ) -> str:
            """Show diff of agent changes. scope='last' for recent, 'all' for everything."""
            if scope == "all":
                return middleware.get_full_diff()
            return middleware.get_diff_since_last_checkpoint()

        def undo_tool(
            runtime: ToolRuntime[None, CheckpointState],
        ) -> str:
            """Undo the last file modification by reverting to the previous checkpoint."""
            return middleware.undo_last_change()

        return [
            StructuredTool.from_function(
                name="show_diff",
                description="Show a diff of changes made by the agent. Use scope='all' for all changes or 'last' for the most recent.",
                func=diff_tool,
            ),
            StructuredTool.from_function(
                name="undo_last_change",
                description="Revert the last file modification to the previous checkpoint state.",
                func=undo_tool,
            ),
        ]

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """Intercept tool calls to create checkpoints before mutations.

        langgraph's `ToolCallRequest` carries a single `tool_call` dict
        (with `name`, `args`, `id`) — not a `tool_calls` list — so this
        inspects that one call and checkpoints before any mutating tool runs.

        Args:
            request: The incoming tool-call request.
            handler: The downstream tool-call handler.

        Returns:
            The handler's result.
        """
        tool_call = request.tool_call or {}
        if tool_call.get("name") in _MUTATING_TOOLS:
            self._create_checkpoint(tool_call["name"], tool_call.get("id", "unknown"))

        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """Async version of `wrap_tool_call`.

        Args:
            request: The incoming tool-call request.
            handler: The downstream async tool-call handler.

        Returns:
            The handler's result.
        """
        tool_call = request.tool_call or {}
        if tool_call.get("name") in _MUTATING_TOOLS:
            await asyncio.to_thread(self._create_checkpoint, tool_call["name"], tool_call.get("id", "unknown"))

        return await handler(request)
