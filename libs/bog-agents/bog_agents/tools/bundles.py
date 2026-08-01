"""Tool-bundle factories — alternatives to tool-contributor middleware.

A *bundle* is a plain function that returns a ``list[BaseTool]`` bound
to the supplied configuration (working directory, options, backend).
Callers pass the bundle to ``create_agent(tools=[*bundle()])`` instead
of constructing a middleware class whose only job is delivering tools.

This is the W4 pattern from the audit pass: middleware whose only hook
is contributing tools shouldn't be middleware. Today the middleware
classes that produce tool lists are kept for backwards compatibility
but delegate to these bundles, so there's one shared source of truth.

Adding a new bundle
-------------------
1. Write a free function in this module: ``def foo_tools_bundle(...) -> list[BaseTool]``.
2. Re-export it from ``bog_agents.tools.__init__``.
3. If a corresponding middleware exists, refactor its ``_build_tools``
   to call the bundle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

# Runtime import: StructuredTool.from_function resolves type hints at
# construction time via pydantic, which evaluates the ``ToolRuntime``
# annotation eagerly even with ``from __future__ import annotations``.
# Moving this into a TYPE_CHECKING block produces ``NameError`` at the
# StructuredTool.from_function callsite.
from langchain.tools import ToolRuntime  # noqa: TC002  # used as runtime type hint by pydantic via StructuredTool
from langchain_core.tools import BaseTool, StructuredTool

from bog_agents.backends.protocol import BACKEND_TYPES  # noqa: TC001  # exposed in public function signatures, kept at runtime for IDE introspection

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


__all__ = [
    "background_shell_tools_bundle",
    "git_tools_bundle",
    "memory_search_tool_bundle",
    "multi_edit_tool",
    "pty_tools_bundle",
    "read_many_files_tool",
]


# ---------------------------------------------------------------------------
# git_tools — the canonical tool-contributor migrated to a bundle
# ---------------------------------------------------------------------------


def git_tools_bundle(
    working_dir: Path | None = None,
    *,
    auto_stage: bool = False,
) -> list[BaseTool]:
    """Return git workflow tools bound to ``working_dir``.

    Args:
        working_dir: Repository root. Defaults to the process CWD.
        auto_stage: When True, ``git_commit`` runs ``git add <file>``
            for every path supplied (even those not yet staged).
            Compatibility shim — matches the historical
            :class:`~bog_agents.middleware.git_tools.GitToolsMiddleware`
            knob, currently unused inside ``git_commit`` itself.

    Returns:
        A list of LangChain ``StructuredTool`` instances ready to pass
        to :func:`bog_agents.create_agent` via the ``tools=`` kwarg.

    The functions are closures over ``working_dir`` rather than methods
    on a class — that's the whole point of the bundle pattern: no
    object lifecycle to manage, no middleware wrapping stack added to
    the model-call path. ``working_dir`` is captured at bundle
    construction so each agent can have its own bound bundle.
    """
    from pathlib import Path as _Path

    from bog_agents.middleware.git_tools import _run_git

    wd = working_dir or _Path.cwd()
    del auto_stage  # currently informational; left for parity with the class shim

    def _git(*args: str, timeout: int = 30) -> str:
        return _run_git(wd, *args, timeout=timeout)

    # NB: every tool below takes ``runtime: ToolRuntime[None, Any]`` as its
    # first parameter because LangChain identifies the runtime-injection
    # slot by the *parameter's type annotation* — and pydantic only sees
    # the parameter if its name does NOT start with an underscore. The
    # closures here don't actually need the runtime value (they bind ``wd``
    # at construction time), so each body opens with ``del runtime`` to
    # silence the unused-argument warning without hiding the contract.

    def git_status(runtime: ToolRuntime[None, Any]) -> str:
        """Show the working tree status including staged, unstaged, and untracked files."""
        del runtime
        return _git("status", "--short", "--branch")

    def git_diff(
        runtime: ToolRuntime[None, Any],
        staged: bool = False,
        path: str | None = None,
    ) -> str:
        """Show changes in the working directory. Use staged=True for staged changes only."""
        del runtime
        args: list[str] = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            args.extend(["--", path])
        return _git(*args)

    def git_log(
        runtime: ToolRuntime[None, Any],
        count: int = 10,
        oneline: bool = True,
    ) -> str:
        """Show recent commit history. Default 10 commits in oneline format."""
        del runtime
        args = ["log", f"-{count}"]
        if oneline:
            args.append("--oneline")
        return _git(*args)

    def git_commit(
        runtime: ToolRuntime[None, Any],
        message: Annotated[str, "Commit message following Conventional Commits format"],
        files: Annotated[
            list[str] | None,
            "Specific files to stage and commit. If None, commits all staged changes.",
        ] = None,
    ) -> str:
        """Create a git commit. Optionally specify files to stage first."""
        del runtime
        if files:
            for fp in files:
                _git("add", fp)
        return _git("commit", "-m", message)

    def git_add(
        runtime: ToolRuntime[None, Any],
        paths: Annotated[list[str], "File paths to stage"],
    ) -> str:
        """Stage files for commit."""
        del runtime
        results: list[str] = []
        for path in paths:
            result = _git("add", path)
            if result:
                results.append(result)
        return "\n".join(results) if results else f"Staged {len(paths)} file(s)"

    def git_branch(
        runtime: ToolRuntime[None, Any],
        name: str | None = None,
        checkout: bool = False,
    ) -> str:
        """List branches, create a new branch, or checkout an existing one."""
        del runtime
        if name is not None:
            from bog_agents.middleware.worktree import _validate_git_ref

            try:
                name = _validate_git_ref(name, label="branch")
            except ValueError as exc:
                return f"Error: {exc}"
        if name and checkout:
            return _git("checkout", "-b", name, "--")
        if name:
            return _git("branch", "--", name)
        if checkout:
            return _git("branch", "-a")
        return _git("branch", "-a", "--sort=-committerdate")

    def git_stash(
        runtime: ToolRuntime[None, Any],
        action: str = "list",
        message: str | None = None,
    ) -> str:
        """Manage git stash. action: 'push', 'pop', 'list', 'show', 'drop'."""
        del runtime
        if action == "push":
            args = ["stash", "push"]
            if message:
                args.extend(["-m", message])
            return _git(*args)
        if action == "pop":
            return _git("stash", "pop")
        if action == "show":
            return _git("stash", "show", "-p")
        if action == "drop":
            return _git("stash", "drop")
        return _git("stash", "list")

    def git_blame(
        runtime: ToolRuntime[None, Any],
        path: Annotated[str, "File path to blame"],
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Show who last modified each line of a file."""
        del runtime
        args = ["blame", "--no-pager"]
        if start_line and end_line:
            args.append(f"-L{start_line},{end_line}")
        args.append(path)
        return _git(*args)

    def git_show(
        runtime: ToolRuntime[None, Any],
        ref: str = "HEAD",
    ) -> str:
        """Show details of a commit. Default shows the latest commit."""
        del runtime
        return _git("show", "--stat", ref)

    return [
        StructuredTool.from_function(
            name="git_status",
            description="Show working tree status.",
            func=git_status,
        ),
        StructuredTool.from_function(
            name="git_diff",
            description="Show file changes. staged=True for staged only.",
            func=git_diff,
        ),
        StructuredTool.from_function(
            name="git_log",
            description="Show commit history.",
            func=git_log,
        ),
        StructuredTool.from_function(
            name="git_commit",
            description="Create a git commit.",
            func=git_commit,
        ),
        StructuredTool.from_function(
            name="git_add",
            description="Stage files for commit.",
            func=git_add,
        ),
        StructuredTool.from_function(
            name="git_branch",
            description="Manage branches.",
            func=git_branch,
        ),
        StructuredTool.from_function(
            name="git_stash",
            description="Manage stash.",
            func=git_stash,
        ),
        StructuredTool.from_function(
            name="git_blame",
            description="Show line-by-line authorship.",
            func=git_blame,
        ),
        StructuredTool.from_function(
            name="git_show",
            description="Show commit details.",
            func=git_show,
        ),
    ]


# ---------------------------------------------------------------------------
# Re-exports of factories that were already in the right shape
# ---------------------------------------------------------------------------
#
# ``multi_edit.create_multi_edit_file_tool`` and
# ``read_many_files.create_read_many_files_tool`` are already
# free-function tool factories — they just happen to live under
# ``middleware/`` for historical reasons. Surfacing them through
# ``bog_agents.tools`` lets callers find them in the obvious place.


def multi_edit_tool(
    backend: BACKEND_TYPES,
    get_backend: Callable[..., Any],
) -> BaseTool:
    """Return the ``multi_edit_file`` tool for batch in-file edits.

    Args:
        backend: A filesystem backend (passed to the underlying factory).
        get_backend: Callable that resolves the backend from a
            :class:`~langchain.tools.ToolRuntime` at invocation time.

    Returns:
        A LangChain ``StructuredTool`` ready to pass via ``tools=``.
    """
    from bog_agents.middleware.multi_edit import create_multi_edit_file_tool

    return create_multi_edit_file_tool(backend, get_backend)


def read_many_files_tool(
    backend: BACKEND_TYPES,
    get_backend: Callable[..., Any],
) -> BaseTool:
    """Return the ``read_many_files`` tool for batched reads.

    Args:
        backend: A filesystem backend (passed to the underlying factory).
        get_backend: Callable that resolves the backend from a
            :class:`~langchain.tools.ToolRuntime` at invocation time.

    Returns:
        A LangChain ``StructuredTool`` ready to pass via ``tools=``.
    """
    from bog_agents.middleware.read_many_files import create_read_many_files_tool

    return create_read_many_files_tool(backend, get_backend)


def background_shell_tools_bundle(backend: Any) -> list[BaseTool]:  # noqa: ANN401 - a LocalShellBackend
    """Return tools to retrieve/manage background shell commands (Tier-1 #1).

    Pairs with `LocalShellBackend(auto_background_after=...)` and
    `execute(background=True)`: when a command is backgrounded, the agent uses
    these tools to read its output, wait for it, or stop it. No-op-safe if the
    backend lacks the background API (returns an empty list).

    Args:
        backend: A `LocalShellBackend` (or any backend exposing
            `poll_background` / `wait_background` / `kill_background` /
            `list_background`).

    Returns:
        A list of `StructuredTool`s, or empty if the backend has no background API.
    """
    if not hasattr(backend, "poll_background"):
        return []

    def _fmt(result: Any) -> str:  # noqa: ANN401 - a BackgroundResult
        if result is None:
            return "No such background task."
        status = "running" if result.running else f"exited (code {result.exit_code})"
        body = result.output or "<no output>"
        return f"[{result.task_id}] {status}\n{body}"

    def poll_background_command(runtime: ToolRuntime[None, Any], task_id: str) -> str:
        """Read the current output + status of a background shell command by its task id."""
        del runtime
        return _fmt(backend.poll_background(task_id))

    def wait_background_command(runtime: ToolRuntime[None, Any], task_id: str, timeout_seconds: float = 60.0) -> str:
        """Wait up to timeout_seconds for a background command to finish, then read it."""
        del runtime
        results = backend.wait_background([task_id], mode="all", timeout=timeout_seconds)
        return _fmt(results[0]) if results else "No such background task."

    def kill_background_command(runtime: ToolRuntime[None, Any], task_id: str) -> str:
        """Stop a running background shell command (and its process tree)."""
        del runtime
        return f"Killed {task_id}." if backend.kill_background(task_id) else "No such background task."

    def list_background_commands(runtime: ToolRuntime[None, Any]) -> str:
        """List all background shell commands and their status."""
        del runtime
        rows = backend.list_background()
        if not rows:
            return "No background commands."
        return "\n".join(f"[{r.task_id}] {'running' if r.running else 'exited'}" for r in rows)

    return [
        StructuredTool.from_function(func=poll_background_command),
        StructuredTool.from_function(func=wait_background_command),
        StructuredTool.from_function(func=kill_background_command),
        StructuredTool.from_function(func=list_background_commands),
    ]


def memory_search_tool_bundle(sources: list[str | Path]) -> list[BaseTool]:
    """Return a `memory_search` tool over the given memory files (Tier-2 #8).

    Builds a `HybridMemoryIndex` from the memory source files (AGENTS.md /
    CLAUDE.md / rules, etc.), chunked on blank lines, so the agent can *search*
    its memory for relevant notes instead of relying only on the whole cascade
    being in context. Keyword mode (FTS5/LIKE) — no embedder needed; the hybrid
    vector path activates only when an embedder is supplied elsewhere.

    Args:
        sources: Memory file paths (unreadable ones are skipped).

    Returns:
        A single-tool list, or empty if no source had readable content.
    """
    from pathlib import Path as _Path

    from bog_agents.hybrid_memory import SOURCE_WORKSPACE, HybridMemoryIndex

    index = HybridMemoryIndex()
    origin: dict[str, str] = {}
    for src in sources:
        path = _Path(src)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for cid in index.add_markdown(text, source=SOURCE_WORKSPACE):
            origin[cid] = path.name
    if not origin:
        return []

    def memory_search(runtime: ToolRuntime[None, Any], query: str, limit: int = 5) -> str:
        """Search your project/user memory (AGENTS.md, CLAUDE.md, rules) for notes relevant to a query."""
        del runtime
        hits = index.search(query, k=limit)
        if not hits:
            return f"No memory matched '{query}'."
        return "\n\n".join(f"[{origin.get(h.chunk.chunk_id, 'memory')}] {h.chunk.text[:300]}" for h in hits)

    return [StructuredTool.from_function(func=memory_search)]


def pty_tools_bundle(controller: Any) -> list[BaseTool]:  # noqa: ANN401 - a PtyController
    """Return tools to drive interactive terminal programs (Tier-2 #6).

    Wraps a `bog_agents.pty_harness.PtyController` so the agent can run
    full-screen TUIs (`vim`, `top`, a REPL): start a session, send vim-notation
    keystrokes, read the screen, and wait on screen conditions.

    Args:
        controller: A `PtyController` holding the agent's sessions.

    Returns:
        A list of `StructuredTool`s (`pty_start` / `pty_send` / `pty_screen` /
        `pty_wait` / `pty_close` / `pty_list`).
    """

    def pty_start(runtime: ToolRuntime[None, Any], name: str, command: str) -> str:
        """Start an interactive program in a new PTY session (e.g. name='vim', command='vim notes.txt')."""
        del runtime
        return controller.start(name, command)

    def pty_send(runtime: ToolRuntime[None, Any], name: str, keys: str) -> str:
        """Send keystrokes to a PTY session in vim notation (e.g. '<Esc>:wq<CR>', 'ihello<Esc>', '<C-c>')."""
        del runtime
        return controller.send(name, keys)

    def pty_screen(runtime: ToolRuntime[None, Any], name: str, tail_lines: int = 40) -> str:
        """Read the current rendered screen of a PTY session."""
        del runtime
        return controller.screen(name, tail_lines=tail_lines)

    def pty_wait(runtime: ToolRuntime[None, Any], name: str, until: str, target: str = "", timeout_seconds: float = 10.0) -> str:
        """Wait for a PTY session's screen condition, then read it. until: text|regex|gone|stable."""
        del runtime
        return controller.wait(name, until, target, timeout_s=timeout_seconds)

    def pty_close(runtime: ToolRuntime[None, Any], name: str) -> str:
        """Close an interactive PTY session."""
        del runtime
        return controller.close(name)

    def pty_list(runtime: ToolRuntime[None, Any]) -> str:
        """List active PTY sessions."""
        del runtime
        return controller.list_sessions()

    return [
        StructuredTool.from_function(func=pty_start),
        StructuredTool.from_function(func=pty_send),
        StructuredTool.from_function(func=pty_screen),
        StructuredTool.from_function(func=pty_wait),
        StructuredTool.from_function(func=pty_close),
        StructuredTool.from_function(func=pty_list),
    ]
