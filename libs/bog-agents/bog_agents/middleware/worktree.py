"""Middleware providing git worktree isolation for parallel agent execution.

Feature #1: Git worktree isolation — each agent session gets its own worktree.
Feature #2: Multi-agent orchestrator — spawn N agents in parallel worktrees.
Feature #3: ParallelWorktreeMiddleware — async parallel execution + conflict detection.

Usage::

    from bog_agents.middleware.worktree import ParallelWorktreeMiddleware

    agent = create_agent(
        model="claude-opus-4-7",
        middleware=[ParallelWorktreeMiddleware(working_dir=Path("/my/project"))],
    )

The agent gains tools to spawn parallel sub-agents in isolated worktrees and
merge results back with conflict detection.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


def _default_agent_factory(prompt: str, working_dir: Path) -> str:
    """Default agent factory for ParallelWorktreeMiddleware.

    Creates a minimal agent with filesystem, git tools, and summarization
    middleware, invokes it with the given prompt, and returns the last AI
    message content.

    Args:
        prompt: Task instructions for the agent.
        working_dir: Working directory (worktree path) for the agent.

    Returns:
        The last AIMessage content as a string.

    Raises:
        Exception: Re-raises any exception from agent invocation so the caller
            can record it as a failed task.
    """
    from bog_agents.graph import create_agent  # lazy import — safe at call time
    from bog_agents.middleware.filesystem import FilesystemMiddleware
    from bog_agents.middleware.git_tools import GitToolsMiddleware
    from bog_agents.middleware.summarization import SummarizationMiddleware

    middleware = [
        FilesystemMiddleware(),
        GitToolsMiddleware(working_dir=working_dir),
        SummarizationMiddleware(),
    ]

    agent = create_agent(
        model=None,
        working_dir=str(working_dir),
        middleware=middleware,
    )
    result = agent.invoke(
        {"messages": [HumanMessage(content=prompt)]},
        config={"recursion_limit": 50},
    )
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return str(msg.content)
    return ""


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


# ---------------------------------------------------------------------------
# Ref-name validation (P1-9)
# ---------------------------------------------------------------------------
#
# Git's command-line parser treats leading ``-`` as flags. A model-supplied
# branch name like ``--exec`` would be interpreted as a flag by older gits
# and is just confusing in newer ones. We reject anything that doesn't pass
# a conservative ref-name check BEFORE invoking git, and add ``--`` to the
# argv where positional args follow options. Fixes REVIEW.md P1-9.

# Git's own rules (man git-check-ref-format) plus our extra "no leading
# dash" constraint. Allows alphanumerics, slashes, hyphens, dots, and
# underscores. Rejects empty names, leading/trailing slashes, ``..``,
# ``@{``, control chars, and anything that starts with ``-``.
_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\-]*$")


def _validate_git_ref(name: str, *, label: str = "ref") -> str:
    """Return *name* if it looks like a safe git ref, else raise ValueError.

    Args:
        name: Candidate ref name.
        label: Human-readable label for the error message.

    Returns:
        The (unchanged) name when safe.

    Raises:
        ValueError: When the name would be interpreted as a flag, contains
            disallowed characters, or otherwise fails the conservative
            check.
    """
    if not isinstance(name, str) or not name:
        msg = f"{label} must be a non-empty string"
        raise ValueError(msg)
    if name.startswith("-"):
        msg = f"{label} {name!r} starts with '-' — refusing (looks like a flag)"
        raise ValueError(msg)
    if ".." in name or "@{" in name or "//" in name or "\\" in name:
        msg = f"{label} {name!r} contains disallowed sequence (.., @{{, //, or \\)"
        raise ValueError(msg)
    if name.endswith((".lock", "/")):
        msg = f"{label} {name!r} ends with disallowed suffix"
        raise ValueError(msg)
    if not _GIT_REF_PATTERN.match(name):
        msg = f"{label} {name!r} contains characters outside [A-Za-z0-9_./-]"
        raise ValueError(msg)
    return name


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

    # P1-9: validate the model-supplied branch name before passing it to
    # git so a hostile or typo'd ref can't be mistaken for a flag.
    _validate_git_ref(branch, label="branch")

    worktree_path = base_dir / branch.replace("/", "-")
    # P1-9: also use ``--`` as the option terminator so any future
    # positional-arg additions to ``git worktree add`` can't be tricked
    # by a leading-dash branch / path.
    result = _run_git(
        repo_dir, "worktree", "add", "-b", branch, "--", str(worktree_path)
    )
    if result.startswith("[exit code"):
        # Branch might already exist, try without -b
        result = _run_git(repo_dir, "worktree", "add", "--", str(worktree_path), branch)

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


# ---------------------------------------------------------------------------
# Merge conflict detection
# ---------------------------------------------------------------------------


def detect_merge_conflicts(repo_dir: Path, source_branch: str, target_branch: str = "HEAD") -> list[str]:
    """Detect files that would conflict if source_branch were merged into target_branch.

    Uses ``git merge-tree`` (git 2.38+) for a dry-run conflict check without
    modifying the working tree.

    Args:
        repo_dir: Repository root directory.
        source_branch: Branch to merge from.
        target_branch: Branch to merge into (default: current HEAD).

    Returns:
        List of file paths that have merge conflicts, or empty list if clean.
    """
    # git merge-tree --write-tree <branch1> <branch2> exits non-zero if conflicts
    result = _run_git(
        repo_dir,
        "merge-tree",
        "--write-tree",
        "--no-messages",
        target_branch,
        source_branch,
        timeout=15,
    )
    if "CONFLICT" in result:
        # Extract conflict file paths
        conflicts = []
        for line in result.splitlines():
            if line.strip().startswith("CONFLICT"):
                # e.g. "CONFLICT (content): Merge conflict in src/auth.py"
                parts = line.split("in ", 1)
                if len(parts) == 2:
                    conflicts.append(parts[1].strip())
        return conflicts
    return []


def _is_trivial_conflict(repo_dir: Path, source_branch: str, target_branch: str) -> bool:
    """Check whether all conflicting changes are whitespace/blank-line only.

    Runs ``git diff -w`` between the two branches; if that diff reports zero
    insertions *and* zero deletions for every conflicting file the conflict is
    considered trivial (formatting-only).

    Args:
        repo_dir: Repository root directory.
        source_branch: Branch to merge from.
        target_branch: Branch to merge into.

    Returns:
        True if all conflicts are whitespace-only, False otherwise.
    """
    # Get changed file list
    changed = _run_git(repo_dir, "diff", f"{target_branch}...{source_branch}", "--name-only")
    if not changed or changed.startswith("[exit code"):
        return False

    # Run diff ignoring whitespace; if it produces no output the diff is whitespace-only
    diff_w = _run_git(repo_dir, "diff", "-w", "--stat", f"{target_branch}...{source_branch}")
    if diff_w.startswith("[exit code"):
        return False

    # A whitespace-only diff has "0 insertions(+), 0 deletions(-)" in summary
    # or produces an empty stat block.  Look for the insertion/deletion line.
    for line in diff_w.splitlines():
        if "insertion" in line or "deletion" in line:
            # e.g. " 1 file changed, 0 insertions(+), 0 deletions(-)"
            nums = re.findall(r"(\d+)\s+insertion|(\d+)\s+deletion", line)
            total = sum(int(n) for pair in nums for n in pair if n)
            if total > 0:
                return False
    return True


def merge_with_conflict_report(
    repo_dir: Path,
    source_branch: str,
    target_branch: str,
    *,
    auto_resolve: bool = False,
    strategy: str = "manual",
) -> dict[str, Any]:
    """Merge source_branch into target_branch with full conflict reporting.

    Args:
        repo_dir: Repository root directory.
        source_branch: Branch to merge from.
        target_branch: Branch to merge into.
        auto_resolve: If True, treat as ``strategy="prefer_source"`` (backward compat).
        strategy: Conflict resolution strategy.

            - ``"prefer_source"`` — use ``-X theirs``; source branch wins.
            - ``"prefer_target"`` — use ``-X ours``; target branch wins.
            - ``"sequential"`` — do not merge; return a sentinel dict with
              ``retry_sequential=True`` so the caller can handle ordering.
            - ``"manual"`` — surface conflicts for human resolution (default).

    Returns:
        Dict with keys: ``success`` (bool), ``conflicts`` (list[str]),
        ``message`` (str), and optionally ``retry_sequential`` (bool).
    """
    # Backward compatibility: auto_resolve=True == prefer_source
    if auto_resolve:
        strategy = "prefer_source"

    # Check out target branch first
    checkout_result = _run_git(repo_dir, "checkout", target_branch)
    if checkout_result.startswith("[exit code"):
        return {
            "success": False,
            "conflicts": [],
            "message": f"Cannot checkout {target_branch}: {checkout_result}",
        }

    # Pre-flight conflict detection
    conflicts = detect_merge_conflicts(repo_dir, source_branch, target_branch)

    if conflicts:
        # Check for trivial (whitespace-only) conflicts and auto-resolve them
        if _is_trivial_conflict(repo_dir, source_branch, target_branch):
            logger.debug(
                "Trivial whitespace-only conflicts detected for %s -> %s; resolving with -X theirs",
                source_branch,
                target_branch,
            )
            merge_args = ["merge", "--no-edit", "-X", "theirs", source_branch]
            merge_result = _run_git(repo_dir, *merge_args)
            success = not merge_result.startswith("[exit code")
            return {
                "success": success,
                "conflicts": [] if success else conflicts,
                "message": merge_result,
            }

        if strategy == "sequential":
            return {
                "success": False,
                "retry_sequential": True,
                "conflicts": conflicts,
                "message": (
                    f"Sequential strategy: {len(conflicts)} conflict(s) detected in "
                    f"{source_branch} -> {target_branch}. "
                    "Skipping merge; retry tasks sequentially to resolve ordering."
                ),
            }

        if strategy == "manual":
            return {
                "success": False,
                "conflicts": conflicts,
                "message": (
                    f"Merge would produce {len(conflicts)} conflict(s):\n"
                    + "\n".join(f"  - {c}" for c in conflicts)
                    + "\n\nResolve conflicts manually or re-run with a different strategy."
                ),
            }

    merge_args = ["merge", "--no-edit"]
    if strategy == "prefer_source":
        merge_args += ["-X", "theirs"]
    elif strategy == "prefer_target":
        merge_args += ["-X", "ours"]
    merge_args.append(source_branch)

    merge_result = _run_git(repo_dir, *merge_args)
    success = not merge_result.startswith("[exit code")

    return {
        "success": success,
        "conflicts": conflicts if not success else [],
        "message": merge_result,
    }


def smart_merge_parallel_tasks(
    mw: ParallelWorktreeMiddleware,
    repo_dir: Path,
    target_branch: str,
    *,
    strategy: str = "prefer_source",
) -> list[dict[str, Any]]:
    """Merge all completed tasks from a ParallelWorktreeMiddleware one by one.

    Args:
        mw: The middleware instance whose completed tasks should be merged.
        repo_dir: Repository root directory.
        target_branch: Branch to merge each task into.
        strategy: Conflict resolution strategy passed to `merge_with_conflict_report`.

    Returns:
        List of merge result dicts, one per completed task (in start-time order).
    """
    results: list[dict[str, Any]] = []
    for task in mw.get_tasks():
        if task.status != "completed":
            continue
        report = merge_with_conflict_report(
            repo_dir,
            task.branch,
            target_branch,
            strategy=strategy,
        )
        if not report["success"] and strategy == "sequential":
            logger.warning(
                "Sequential merge conflict for task %s (%s): %s",
                task.task_id,
                task.label,
                report.get("message", ""),
            )
        results.append({"task_id": task.task_id, "label": task.label, **report})
    return results


# ---------------------------------------------------------------------------
# Parallel execution tracking
# ---------------------------------------------------------------------------


@dataclass
class WorktreeTask:
    """A tracked parallel agent task in an isolated worktree.

    Attributes:
        task_id: Unique identifier.
        label: Human-readable description.
        prompt: Task instructions for the agent.
        branch: Git branch name for this task's worktree.
        worktree: WorktreeInfo for this task.
        status: 'pending' | 'running' | 'completed' | 'failed'.
        result: Output text when completed.
        started_at: Epoch timestamp when task started.
        finished_at: Epoch timestamp when task finished.
        error: Error message if failed.
    """

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    label: str = ""
    prompt: str = ""
    branch: str = ""
    worktree: WorktreeInfo | None = None
    status: str = "pending"
    result: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    @property
    def duration_secs(self) -> float | None:
        """Return elapsed time in seconds, or None if not started."""
        if self.started_at == 0.0:
            return None
        end = self.finished_at or time.monotonic()
        return end - self.started_at


def format_worktree_status(tasks: list[WorktreeTask]) -> str:
    """Format a list of parallel tasks for TUI display.

    Args:
        tasks: List of WorktreeTask objects.

    Returns:
        Human-readable status panel string.
    """
    if not tasks:
        return "No parallel worktree tasks running."

    lines = [f"Parallel Worktree Tasks ({len(tasks)}):"]
    for task in tasks:
        icon = {"pending": "○", "running": "◎", "completed": "✓", "failed": "✗"}.get(task.status, "?")
        dur = task.duration_secs
        dur_str = f" ({dur:.0f}s)" if dur is not None else ""
        label = task.label or task.prompt[:50]
        lines.append(f"  {icon} [{task.task_id}] {label}{dur_str}  [{task.status}]")
        if task.branch:
            lines.append(f"    branch: {task.branch}")
        if task.error:
            lines.append(f"    error: {task.error[:80]}")
        if task.result and task.status == "completed":
            preview = task.result[:100].replace("\n", " ")
            lines.append(f"    result: {preview}...")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ParallelWorktreeMiddleware
# ---------------------------------------------------------------------------


class ParallelWorktreeState(TypedDict):
    """LangGraph state for parallel worktree middleware."""


class ParallelWorktreeMiddleware(AgentMiddleware[ParallelWorktreeState, ContextT, ResponseT]):
    """Spawn parallel sub-agents in isolated git worktrees with merge management.

    Each sub-agent task runs in its own git worktree (separate directory +
    branch) so there are no file conflicts between agents. Results are merged
    back using ``merge_with_conflict_report`` which surfaces conflicts clearly.

    Compared to ``WorktreeMiddleware`` (which provides manual tools), this
    middleware adds:

    - Async parallel execution via ``asyncio.gather``
    - Automatic worktree cleanup after merge
    - Pre-merge conflict detection
    - TUI-ready status tracking

    Args:
        working_dir: Repository root directory.
        agent_factory: Callable that takes a prompt and working_dir and returns
            a result string. If None, tasks are tracked but not auto-executed.
        max_parallel: Maximum concurrent worktree tasks (default 4).
        branch_prefix: Prefix for auto-generated branch names.
        auto_cleanup: Remove worktrees when tasks complete.
    """

    state_schema = ParallelWorktreeState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        agent_factory: Callable[[str, Path], Any] | None = _default_agent_factory,
        max_parallel: int = 4,
        branch_prefix: str = "bog-agent-",
        auto_cleanup: bool = True,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._agent_factory = agent_factory
        self._max_parallel = max_parallel
        self._branch_prefix = branch_prefix
        self._auto_cleanup = auto_cleanup
        self._tasks: dict[str, WorktreeTask] = {}
        self._semaphore: asyncio.Semaphore | None = None
        # Strong references to in-flight background tasks. asyncio's docs
        # explicitly warn that without a stable reference the task may be
        # garbage-collected mid-execution. See P0-I in REVIEW.md.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._tools = self._build_tools()

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Get or create the concurrency semaphore (event-loop-safe)."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_parallel)
        return self._semaphore

    def get_tasks(self) -> list[WorktreeTask]:
        """Return all tracked tasks, sorted by start time."""
        return sorted(self._tasks.values(), key=lambda t: t.started_at or 0.0)

    def get_task(self, task_id: str) -> WorktreeTask | None:
        """Return a task by ID."""
        return self._tasks.get(task_id)

    @property
    def tools(self) -> list[BaseTool]:
        """Expose parallel worktree tools."""
        return self._tools

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_branch_name(self, label: str) -> str:
        """Create a safe git branch name from a task label."""
        safe = label.lower().replace(" ", "-").replace("/", "-")
        safe = "".join(c for c in safe if c.isalnum() or c in "-_")[:40]
        short_id = uuid.uuid4().hex[:6]
        return f"{self._branch_prefix}{safe}-{short_id}"

    async def _create_task(self, *, label: str, prompt: str, repo_root: Path | None = None) -> WorktreeTask:
        """Create and register a new WorktreeTask without starting it.

        Args:
            label: Human-readable label for the task.
            prompt: The prompt to run in the worktree.
            repo_root: Optional override for the working directory.

        Returns:
            The created WorktreeTask.
        """
        task = WorktreeTask(
            task_id=uuid.uuid4().hex[:12],
            label=label,
            prompt=prompt,
            branch=self._make_branch_name(label),
        )
        if repo_root is not None:
            self._working_dir = repo_root
        self._tasks[task.task_id] = task
        return task

    async def _run_task_in_worktree(self, task: WorktreeTask) -> None:
        """Execute a single task in its worktree.

        Args:
            task: The task to execute.
        """
        async with self._get_semaphore():
            task.status = "running"
            task.started_at = time.monotonic()

            try:
                # Create the worktree
                wt = await asyncio.to_thread(create_worktree, self._working_dir, task.branch)
                task.worktree = wt

                if self._agent_factory is not None:
                    # Run the factory (sync) in a thread
                    result = await asyncio.to_thread(self._agent_factory, task.prompt, wt.path)
                    task.result = str(result)

                task.status = "completed"
            except Exception as exc:
                task.status = "failed"
                task.error = str(exc)
                logger.warning("Worktree task %s failed: %s", task.task_id, exc)
            finally:
                task.finished_at = time.monotonic()

                if self._auto_cleanup and task.worktree is not None:
                    try:
                        await asyncio.to_thread(remove_worktree, self._working_dir, task.worktree.path)
                    except Exception as exc:
                        logger.debug("Failed to clean up worktree: %s", exc)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _build_tools(self) -> list[BaseTool]:
        """Build parallel worktree tools exposed to the agent."""
        mw = self

        def spawn_parallel_tasks(
            runtime: ToolRuntime[None, ParallelWorktreeState],
            tasks: Annotated[
                str,
                'JSON array of task objects with \'label\' and \'prompt\' keys. Example: [{"label": "auth", "prompt": "Refactor auth.py"}]',
            ],
        ) -> str:
            """Spawn multiple agent tasks in parallel git worktrees.

            Each task gets its own isolated branch and directory. Tasks run
            concurrently up to the max_parallel limit.
            """
            import json as _json

            try:
                task_specs = _json.loads(tasks)
            except _json.JSONDecodeError as exc:
                return f"Invalid JSON: {exc}"

            created_tasks: list[WorktreeTask] = []
            for spec in task_specs:
                label = str(spec.get("label", "task"))
                prompt = str(spec.get("prompt", ""))
                branch = mw._make_branch_name(label)
                task = WorktreeTask(label=label, prompt=prompt, branch=branch)
                mw._tasks[task.task_id] = task
                created_tasks.append(task)

            # Fire and forget — tasks run in the background. Keep a strong
            # reference on ``mw._background_tasks`` so asyncio doesn't GC the
            # task mid-flight. The done-callback discards from the set so
            # memory stays bounded over the session. (Fixes P0-I.)
            if created_tasks:
                bg = asyncio.ensure_future(
                    asyncio.gather(
                        *(mw._run_task_in_worktree(t) for t in created_tasks),
                        return_exceptions=True,
                    )
                )
                mw._background_tasks.add(bg)
                bg.add_done_callback(mw._background_tasks.discard)

            ids = ", ".join(t.task_id for t in created_tasks)
            return f"Spawned {len(created_tasks)} parallel task(s): {ids}\nUse `worktree_status` to monitor progress."

        def worktree_status(
            runtime: ToolRuntime[None, ParallelWorktreeState],
        ) -> str:
            """Show the status of all parallel worktree tasks."""
            return format_worktree_status(mw.get_tasks())

        def merge_task_results(
            runtime: ToolRuntime[None, ParallelWorktreeState],
            task_ids: Annotated[
                str,
                "Comma-separated task IDs to merge, or 'all' for all completed tasks",
            ],
            target_branch: Annotated[str, "Branch to merge into"] = "HEAD",
            auto_resolve: Annotated[bool, "Auto-resolve conflicts favouring the task branch"] = False,
        ) -> str:
            """Merge completed worktree tasks back into the target branch."""
            if task_ids.strip().lower() == "all":
                candidates = [t for t in mw._tasks.values() if t.status == "completed"]
            else:
                ids = [i.strip() for i in task_ids.split(",")]
                candidates = [mw._tasks[i] for i in ids if i in mw._tasks]

            if not candidates:
                return "No completed tasks found to merge."

            reports: list[str] = []
            for task in candidates:
                report = merge_with_conflict_report(
                    mw._working_dir,
                    task.branch,
                    target_branch,
                    auto_resolve=auto_resolve,
                )
                status = "✓ merged" if report["success"] else "✗ conflicts"
                reports.append(f"{status} [{task.task_id}] {task.label}: {report['message'][:200]}")

            return "\n".join(reports)

        def cancel_task(
            runtime: ToolRuntime[None, ParallelWorktreeState],
            task_id: Annotated[str, "Task ID to cancel"],
        ) -> str:
            """Cancel a pending or running worktree task."""
            task = mw._tasks.get(task_id)
            if task is None:
                return f"Task '{task_id}' not found."
            if task.status == "completed":
                return f"Task '{task_id}' already completed."
            task.status = "failed"
            task.error = "Cancelled by user"
            task.finished_at = time.monotonic()
            return f"Task '{task_id}' cancelled."

        return [
            StructuredTool.from_function(
                name="spawn_parallel_tasks",
                description="Spawn multiple agent tasks in parallel git worktrees.",
                func=spawn_parallel_tasks,
            ),
            StructuredTool.from_function(
                name="worktree_status",
                description="Show status of all parallel worktree tasks.",
                func=worktree_status,
            ),
            StructuredTool.from_function(
                name="merge_task_results",
                description="Merge completed worktree tasks into the target branch.",
                func=merge_task_results,
            ),
            StructuredTool.from_function(
                name="cancel_task",
                description="Cancel a pending or running worktree task.",
                func=cancel_task,
            ),
        ]
