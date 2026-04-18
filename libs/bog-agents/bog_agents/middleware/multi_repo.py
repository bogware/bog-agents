"""Multi-repository context middleware (.bog-agents/workspace.toml).

Allows agents to reference other repositories with @repo:name mentions.
Injects repo maps, recent changes, and cross-repo symbol search.

Workspace config (.bog-agents/workspace.toml):
    [repos]
    auth-service = { path = "../auth-service", description = "Auth microservice" }
    frontend = { path = "/absolute/path/to/frontend", description = "React frontend" }

    [settings]
    shared_embeddings = false
    max_repos = 10
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections import defaultdict
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

_WORKSPACE_FILE = ".bog-agents/workspace.toml"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RepoConfig:
    """Configuration for a single repository in the workspace.

    Attributes:
        name: Logical name used to reference this repo (e.g. ``auth-service``).
        path: Absolute path to the repository root on disk.
        description: Human-readable description of the repository's purpose.
    """

    name: str
    path: Path
    description: str = ""

    @property
    def exists(self) -> bool:
        """Return True if the repository path exists on disk.

        Returns:
            True when ``path`` resolves to an existing directory.
        """
        return self.path.is_dir()


# ---------------------------------------------------------------------------
# Workspace loading
# ---------------------------------------------------------------------------


def _parse_repos_regex(text: str, project_root: Path) -> dict[str, RepoConfig]:
    """Parse the ``[repos]`` section using a simple regex fallback.

    Used when neither ``tomllib`` nor ``tomli`` is available (Python < 3.11
    without the back-port installed).

    Args:
        text: Raw contents of the workspace TOML file.
        project_root: Repository root used to resolve relative paths.

    Returns:
        Mapping of repo name to ``RepoConfig``.
    """
    repos: dict[str, RepoConfig] = {}

    # Find the [repos] section — everything until the next [section] header or EOF
    repos_block_match = re.search(r"\[repos\](.*?)(?=\n\[|\Z)", text, re.DOTALL)
    if not repos_block_match:
        return repos

    block = repos_block_match.group(1)

    # Match lines like: name = { path = "...", description = "..." }
    # or:               name = { path = "..." }
    entry_pattern = re.compile(
        r"""^\s*(?P<name>[\w\-]+)\s*=\s*\{\s*"""
        r"""path\s*=\s*["'](?P<path>[^"']+)["']"""
        r"""(?:\s*,\s*description\s*=\s*["'](?P<desc>[^"']*)["'])?"""
        r"""\s*\}""",
        re.MULTILINE,
    )

    for m in entry_pattern.finditer(block):
        name = m.group("name")
        raw_path = m.group("path")
        description = m.group("desc") or ""

        resolved = Path(raw_path)
        if not resolved.is_absolute():
            resolved = (project_root / resolved).resolve()

        repos[name] = RepoConfig(name=name, path=resolved, description=description)

    return repos


def load_workspace(project_root: Path) -> dict[str, RepoConfig]:
    """Parse the workspace TOML file and return all configured repositories.

    Attempts to use ``tomllib`` (stdlib, Python 3.11+) or ``tomli`` (back-port).
    Falls back to a simple regex parser when neither is available.

    Relative repository paths are resolved relative to ``project_root``.

    Args:
        project_root: Root directory of the current project. Used to locate
            ``.bog-agents/workspace.toml`` and to resolve relative repo paths.

    Returns:
        Mapping of repo name to ``RepoConfig``. Returns an empty dict when the
        workspace file does not exist or cannot be parsed.
    """
    workspace_path = project_root / _WORKSPACE_FILE
    if not workspace_path.exists():
        return {}

    try:
        text = workspace_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("MultiRepo: could not read %s", workspace_path)
        return {}

    # Attempt stdlib tomllib (3.11+), then tomli back-port, then regex fallback
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            logger.warning("MultiRepo: failed to parse %s with tomllib", workspace_path, exc_info=True)
            return {}

        repos: dict[str, RepoConfig] = {}
        for name, entry in data.get("repos", {}).items():
            if not isinstance(entry, dict) or "path" not in entry:
                continue
            raw_path = entry["path"]
            description = entry.get("description", "")
            resolved = Path(raw_path)
            if not resolved.is_absolute():
                resolved = (project_root / resolved).resolve()
            repos[name] = RepoConfig(name=name, path=resolved, description=description)
        return repos

    # Regex fallback
    return _parse_repos_regex(text, project_root)


# ---------------------------------------------------------------------------
# Repository introspection helpers
# ---------------------------------------------------------------------------


def get_repo_map(repo: RepoConfig, max_files: int = 50) -> str:
    """Return a directory-grouped listing of tracked files in a repository.

    Uses ``git ls-files`` to respect ``.gitignore`` and only list tracked
    source files. Results are grouped by directory for readability.

    Args:
        repo: Repository to inspect.
        max_files: Maximum number of files to include in the output.

    Returns:
        Formatted string listing files grouped by directory, or an error/
        empty message when the repository cannot be queried.
    """
    if not repo.exists:
        return f"[{repo.name}] Repository not found at {repo.path}"

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo.path,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("MultiRepo: git ls-files failed for %s: %s", repo.name, exc)
        return f"[{repo.name}] Could not list files: {exc}"

    all_files = [f for f in result.stdout.splitlines() if f]
    shown_files = all_files[:max_files]

    # Group by directory
    by_dir: dict[str, list[str]] = defaultdict(list)
    for f in shown_files:
        directory = str(Path(f).parent)
        by_dir[directory].append(Path(f).name)

    lines: list[str] = [f"## {repo.name}"]
    if repo.description:
        lines.append(f"_{repo.description}_")
    lines.append("")

    for directory in sorted(by_dir):
        display_dir = directory if directory != "." else "(root)"
        lines.append(f"  {display_dir}/")
        for filename in sorted(by_dir[directory]):
            lines.append(f"    {filename}")

    if len(all_files) > max_files:
        lines.append(f"\n  ... ({len(all_files) - max_files} more files not shown)")

    return "\n".join(lines)


def get_recent_changes(repo: RepoConfig, n_commits: int = 5) -> str:
    """Return a summary of recent commits in a repository.

    Args:
        repo: Repository to inspect.
        n_commits: Number of recent commits to include.

    Returns:
        Formatted string of recent commits in oneline format, or an error
        message when the repository cannot be queried.
    """
    if not repo.exists:
        return f"[{repo.name}] Repository not found at {repo.path}"

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"-n{n_commits}"],
            cwd=repo.path,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("MultiRepo: git log failed for %s: %s", repo.name, exc)
        return f"[{repo.name}] Could not retrieve commits: {exc}"

    log_output = result.stdout.strip()
    if not log_output:
        return f"[{repo.name}] No commits found."

    lines = [f"## {repo.name} — recent changes"]
    for line in log_output.splitlines():
        lines.append(f"  {line}")
    return "\n".join(lines)


def search_across_repos(query: str, repos: dict[str, RepoConfig], max_per_repo: int = 5) -> str:
    """Search for a query string across all configured repositories using ripgrep.

    For each repository that exists on disk, runs ``rg -l <query>`` to find
    files containing the query, then collects up to ``max_per_repo`` matches.

    Args:
        query: Search query (passed verbatim to ripgrep as a literal pattern).
        repos: Mapping of repo name to ``RepoConfig`` to search.
        max_per_repo: Maximum number of matching files to report per repository.

    Returns:
        Formatted results string showing matched files per repository, or a
        ``"No results found"`` message when nothing matches.
    """
    if not repos:
        return "No repositories configured in workspace."

    sections: list[str] = []

    for name, repo in repos.items():
        if not repo.exists:
            logger.debug("MultiRepo: skipping non-existent repo %s at %s", name, repo.path)
            continue

        try:
            result = subprocess.run(
                ["rg", "-F", "-l", query, str(repo.path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("MultiRepo: rg failed for repo %s: %s", name, exc)
            continue

        matched_files = [f for f in result.stdout.splitlines() if f]
        if not matched_files:
            continue

        shown = matched_files[:max_per_repo]
        # Make paths relative to the repo root for readability
        display_paths: list[str] = []
        for f in shown:
            try:
                display_paths.append(str(Path(f).relative_to(repo.path)))
            except ValueError:
                display_paths.append(f)

        suffix = f" (+{len(matched_files) - max_per_repo} more)" if len(matched_files) > max_per_repo else ""
        sections.append(f"{name}: {', '.join(display_paths)}{suffix}")

    if not sections:
        return "No results found."

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class MultiRepoState(TypedDict):
    """LangGraph state shard for multi-repository middleware."""


class MultiRepoMiddleware(AgentMiddleware[MultiRepoState, ContextT, ResponseT]):
    """Middleware that provides multi-repository context and cross-repo tools.

    Reads a workspace configuration from ``.bog-agents/workspace.toml`` in the
    project root and exposes tools that allow the agent to explore other
    repositories referenced in that file.

    Workspace file format::

        [repos]
        auth-service = { path = "../auth-service", description = "Auth microservice" }
        frontend = { path = "/absolute/path/to/frontend", description = "React frontend" }

        [settings]
        shared_embeddings = false
        max_repos = 10

    The repo list is refreshed from disk at most once every ``reload_interval``
    seconds (lazy, triggered on the next tool call or ``repos`` property access).

    Args:
        project_root: Root directory containing ``.bog-agents/workspace.toml``.
            Defaults to ``Path.cwd()``.
        reload_interval: Seconds between automatic workspace reloads. Default 60.
    """

    state_schema = MultiRepoState

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        reload_interval: float = 60.0,
    ) -> None:
        self._project_root = project_root or Path.cwd()
        self._reload_interval = reload_interval
        self._repos: dict[str, RepoConfig] = {}
        self._last_load: float = 0.0
        self._tools = self._build_tools()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def repos(self) -> dict[str, RepoConfig]:
        """Return the current set of configured repositories.

        Reloads the workspace file from disk if the reload interval has elapsed.

        Returns:
            Mapping of repo name to ``RepoConfig``.
        """
        if time.monotonic() - self._last_load > self._reload_interval:
            self._load_repos()
        return self._repos

    @property
    def tools(self) -> list[BaseTool]:
        """Return the tools provided by this middleware.

        Returns:
            List of ``BaseTool`` instances registered for the agent.
        """
        return self._tools

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_repos(self) -> None:
        """Reload the workspace configuration from disk.

        Updates ``_repos`` and ``_last_load`` unconditionally. Errors during
        loading are logged and an empty repo set is used instead.
        """
        self._repos = load_workspace(self._project_root)
        self._last_load = time.monotonic()
        logger.debug("MultiRepo: loaded %d repo(s) from workspace", len(self._repos))

    def _build_tools(self) -> list[BaseTool]:
        """Construct the agent tools provided by this middleware.

        Returns two ``StructuredTool`` instances:

        * ``inject_repo_context`` — returns a repo map and recent commit summary.
        * ``search_across_repos`` — searches for a query across all repos.

        Returns:
            List of built ``BaseTool`` instances.
        """
        mw = self

        def inject_repo_context(
            runtime: ToolRuntime[None, MultiRepoState],
            repo_name: Annotated[str, "Name of repo from workspace.toml"],
        ) -> str:
            """Retrieve context for a named repository from the workspace.

            Returns the file tree (via git ls-files) and the 5 most recent
            commits for the specified repository. Use this when the user
            references an external repo with @repo:name or asks about another
            service in the workspace.
            """
            repos = mw.repos
            repo = repos.get(repo_name)
            if repo is None:
                available = ", ".join(repos.keys()) if repos else "none"
                return f"Repository '{repo_name}' not found in workspace. Available: {available}"

            repo_map_text = get_repo_map(repo)
            recent_changes_text = get_recent_changes(repo)
            return f"{repo_map_text}\n\n{recent_changes_text}"

        def search_repos(
            runtime: ToolRuntime[None, MultiRepoState],
            query: Annotated[str, "Search query"],
        ) -> str:
            """Search for a symbol, string, or pattern across all workspace repositories.

            Uses ripgrep to find files containing the query in each configured
            repository. Returns a summary of matching files grouped by repository.
            Useful for cross-repo dependency and usage analysis.
            """
            repos = mw.repos
            if not repos:
                return "No repositories configured in .bog-agents/workspace.toml."
            result = search_across_repos(query, repos)
            return result or "No results found."

        return [
            StructuredTool.from_function(
                name="inject_repo_context",
                description=(
                    "Get file tree and recent commits for a named repository in the workspace. "
                    "Use when the user references an external service with @repo:name."
                ),
                func=inject_repo_context,
            ),
            StructuredTool.from_function(
                name="search_across_repos",
                description=(
                    "Search for a symbol, string, or pattern across all configured workspace repositories. "
                    "Returns matching file paths grouped by repository."
                ),
                func=search_repos,
            ),
        ]
