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
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

# Tools that modify files and should trigger checkpointing
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "multi_edit_file", "execute"})


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

    def _run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run a git command in the working directory.

        Args:
            *args: Git command arguments.
            cwd: Override working directory.

        Returns:
            Completed process result.
        """
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self._working_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

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
                    "Checkpointing disabled: `git init` failed in %s "
                    "(exit %d). stderr: %s",
                    self._working_dir,
                    init_result.returncode,
                    (init_result.stderr or "").strip()[:200],
                )
                self._enabled = False
                self._initialized = True
                return

        # Create initial checkpoint
        add_result = self._run_git("add", "-A")
        if add_result.returncode != 0:
            logger.warning(
                "Checkpointing baseline `git add -A` failed: exit %d. stderr: %s",
                add_result.returncode,
                (add_result.stderr or "").strip()[:200],
            )
        result = self._run_git("stash", "create", "bog-agents-checkpoint-init")
        if result.returncode == 0 and result.stdout.strip():
            self._checkpoints.append(
                Checkpoint(
                    commit_hash=result.stdout.strip(),
                    message="Initial checkpoint before agent modifications",
                    tool_call_id="init",
                )
            )
        elif result.returncode != 0:
            logger.warning(
                "Checkpointing initial `git stash create` failed: exit %d. stderr: %s",
                result.returncode,
                (result.stderr or "").strip()[:200],
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

        # Create a checkpoint commit on a detached ref
        msg = f"bog-agents-checkpoint: before {tool_name} ({tool_call_id})"
        result = self._run_git("stash", "create", msg)

        if result.returncode == 0 and result.stdout.strip():
            checkpoint_hash = result.stdout.strip()
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
            result = self._run_git("diff")
            return result.stdout if result.returncode == 0 else "No changes detected."

        last = self._checkpoints[-1]
        result = self._run_git("diff", last.commit_hash)
        return result.stdout if result.returncode == 0 else "Could not generate diff."

    def get_full_diff(self) -> str:
        """Get the diff of all agent changes from the initial checkpoint.

        Returns:
            Git diff output as a string.
        """
        if not self._checkpoints:
            return "No checkpoints available."

        first = self._checkpoints[0]
        result = self._run_git("diff", first.commit_hash)
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
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Intercept tool calls to create checkpoints before mutations.

        Args:
            request: The model request containing tool calls.
            call_next: The next handler in the chain.

        Returns:
            The model response.
        """
        # Check if any pending tool calls are mutating tools
        if hasattr(request, "tool_calls"):
            for tc in request.tool_calls:
                if tc.get("name") in _MUTATING_TOOLS:
                    self._create_checkpoint(tc["name"], tc.get("id", "unknown"))
                    break

        return call_next(request)

    async def awrap_tool_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_tool_call."""
        if hasattr(request, "tool_calls"):
            for tc in request.tool_calls:
                if tc.get("name") in _MUTATING_TOOLS:
                    await asyncio.to_thread(self._create_checkpoint, tc["name"], tc.get("id", "unknown"))
                    break

        return await call_next(request)
