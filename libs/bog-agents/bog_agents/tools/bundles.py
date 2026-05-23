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
    "git_tools_bundle",
    "multi_edit_tool",
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
