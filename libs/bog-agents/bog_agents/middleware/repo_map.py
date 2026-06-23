"""Semantic repository map middleware — structural codebase index for LLM context.

Builds a fast, LLM-consumable map of the entire repo: classes, functions,
signatures, and import relationships. Uses regex-based AST extraction (no
extra dependencies) with file-hash-based incremental caching stored in
``.bog-agents/repomap.json``.

The map is injected into the system prompt at the start of each session and
is refreshable via the ``repo_map`` tool during the conversation.

Usage via middleware::

    from bog_agents.middleware.repo_map import RepoMapMiddleware

    agent = create_agent(
        model="claude-opus-4-7",
        middleware=[RepoMapMiddleware()],
    )

Usage standalone (e.g. from a CLI command)::

    from bog_agents.middleware.repo_map import build_repo_map_cached

    map_text = build_repo_map_cached(Path("/my/project"))
    print(map_text)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language patterns — regex-based symbol extraction per extension
# ---------------------------------------------------------------------------

_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    ".py": [
        ("class", re.compile(r"^class\s+(\w+)(?:\(.*?\))?:", re.MULTILINE)),
        ("function", re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)),
        ("import", re.compile(r"^(?:from\s+(\S+)\s+)?import\s+(.+)", re.MULTILINE)),
    ],
    ".js": [
        ("class", re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)),
        ("const_fn", re.compile(r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE)),
        ("import", re.compile(r"^import\s+.+\s+from\s+['\"](.+?)['\"]", re.MULTILINE)),
    ],
    ".ts": [
        ("class", re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)),
        ("const_fn", re.compile(r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\(", re.MULTILINE)),
        ("interface", re.compile(r"^(?:export\s+)?interface\s+(\w+)", re.MULTILINE)),
        ("type", re.compile(r"^(?:export\s+)?type\s+(\w+)\s*=", re.MULTILINE)),
        ("import", re.compile(r"^import\s+.+\s+from\s+['\"](.+?)['\"]", re.MULTILINE)),
    ],
    ".rs": [
        ("struct", re.compile(r"^pub\s+struct\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(r"^pub\s+enum\s+(\w+)", re.MULTILINE)),
        ("fn", re.compile(r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)),
        ("trait", re.compile(r"^pub\s+trait\s+(\w+)", re.MULTILINE)),
        ("impl", re.compile(r"^impl(?:<.*?>)?\s+(\w+)", re.MULTILINE)),
    ],
    ".go": [
        ("struct", re.compile(r"^type\s+(\w+)\s+struct", re.MULTILINE)),
        ("interface", re.compile(r"^type\s+(\w+)\s+interface", re.MULTILINE)),
        ("func", re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(", re.MULTILINE)),
    ],
    ".java": [
        ("class", re.compile(r"^(?:public\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^(?:public\s+)?interface\s+(\w+)", re.MULTILINE)),
        ("method", re.compile(r"^\s+(?:public|private|protected)\s+\S+\s+(\w+)\s*\(", re.MULTILINE)),
    ],
    ".rb": [
        ("class", re.compile(r"^class\s+(\w+)", re.MULTILINE)),
        ("module", re.compile(r"^module\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^\s+def\s+(\w+)", re.MULTILINE)),
    ],
    ".php": [
        ("class", re.compile(r"^(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^interface\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^(?:public|private|protected|static|\s)*function\s+(\w+)", re.MULTILINE)),
    ],
    ".swift": [
        ("class", re.compile(r"^(?:public\s+)?class\s+(\w+)", re.MULTILINE)),
        ("struct", re.compile(r"^(?:public\s+)?struct\s+(\w+)", re.MULTILINE)),
        ("protocol", re.compile(r"^(?:public\s+)?protocol\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^\s+(?:public\s+)?func\s+(\w+)", re.MULTILINE)),
    ],
    ".kt": [
        ("class", re.compile(r"^(?:data\s+)?class\s+(\w+)", re.MULTILINE)),
        ("object", re.compile(r"^object\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^interface\s+(\w+)", re.MULTILINE)),
        ("function", re.compile(r"^\s+fun\s+(\w+)", re.MULTILINE)),
    ],
    ".cs": [
        ("class", re.compile(r"^(?:public\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^(?:public\s+)?interface\s+(\w+)", re.MULTILINE)),
        ("method", re.compile(r"^\s+(?:public|private|protected|internal|static|\s)*\s+\w+\s+(\w+)\s*\(", re.MULTILINE)),
    ],
}

_EXT_MAP: dict[str, str] = {
    ".tsx": ".ts",
    ".jsx": ".js",
    ".mjs": ".js",
    ".cjs": ".js",
    ".mts": ".ts",
    ".cts": ".ts",
}

_DEFAULT_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".mjs",
        ".cjs",
        ".mts",
        ".cts",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".cs",
    }
)

_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "venv",
        ".venv",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        "coverage",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "vendor",
        "site-packages",
    }
)

_MAX_FILE_SIZE = 500_000  # 500KB
_MAX_FILES = 5000
_CACHE_VERSION = 2


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------


class FileSymbols:
    """Extracted symbols from a single file."""

    __slots__ = ("classes", "functions", "imports", "mtime_hash", "other", "path", "size")

    def __init__(self, path: str) -> None:
        self.path = path
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.imports: list[str] = []
        self.other: dict[str, list[str]] = defaultdict(list)
        self.size: int = 0
        self.mtime_hash: str = ""

    def to_summary(self, *, max_functions: int = 10) -> str:
        """Return a compact summary line for LLM context injection."""
        parts = [self.path]
        if self.classes:
            parts.append(f"  classes: {', '.join(self.classes)}")
        if self.functions:
            fns = self.functions[:max_functions]
            suffix = f" (+{len(self.functions) - max_functions} more)" if len(self.functions) > max_functions else ""
            parts.append(f"  functions: {', '.join(fns)}{suffix}")
        for category, items in self.other.items():
            shown = items[:5]
            suffix = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
            parts.append(f"  {category}: {', '.join(shown)}{suffix}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict for caching."""
        return {
            "path": self.path,
            "classes": self.classes,
            "functions": self.functions,
            "imports": self.imports,
            "other": dict(self.other),
            "size": self.size,
            "mtime_hash": self.mtime_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSymbols:
        """Deserialize from a cached dict."""
        sym = cls(data["path"])
        sym.classes = data.get("classes", [])
        sym.functions = data.get("functions", [])
        sym.imports = data.get("imports", [])
        sym.other = defaultdict(list, data.get("other", {}))
        sym.size = data.get("size", 0)
        sym.mtime_hash = data.get("mtime_hash", "")
        return sym


def _file_hash(path: Path) -> str:
    """Return a fast fingerprint for a file (mtime + size, no read needed)."""
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return ""


def _extract_symbols(path: Path, content: str, mtime_hash: str = "") -> FileSymbols:
    """Extract symbols from a file using language-specific regex patterns."""
    symbols = FileSymbols(str(path))
    symbols.mtime_hash = mtime_hash
    ext = path.suffix.lower()
    patterns_key = _EXT_MAP.get(ext, ext)
    patterns = _PATTERNS.get(patterns_key, [])

    for category, pattern in patterns:
        for match in pattern.finditer(content):
            name = match.group(1)
            if not name:
                continue
            if category in ("class", "struct", "enum", "interface", "type", "trait", "protocol", "module", "object"):
                symbols.classes.append(name)
            elif category in ("function", "fn", "func", "method", "const_fn"):
                symbols.functions.append(name)
            elif category in ("import", "use"):
                symbols.imports.append(name)
            else:
                symbols.other[category].append(name)

    return symbols


# ---------------------------------------------------------------------------
# Incremental cache
# ---------------------------------------------------------------------------


class RepoMapCache:
    """Persistent repo map index with incremental file-hash-based updates.

    Stores extracted symbols in ``.bog-agents/repomap.json`` and only
    re-parses files that have changed since the last run.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache_path = root / ".bog-agents" / "repomap.json"
        self._entries: dict[str, FileSymbols] = {}
        self._built_at: float = 0.0
        self._loaded = False

    def load(self) -> None:
        """Load the on-disk cache if present and version-compatible."""
        if not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if data.get("version") != _CACHE_VERSION:
                return
            self._built_at = data.get("built_at", 0.0)
            for entry_data in data.get("entries", []):
                sym = FileSymbols.from_dict(entry_data)
                self._entries[sym.path] = sym
            self._loaded = True
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            logger.debug("RepoMap cache invalid or missing — will rebuild.", exc_info=True)

    def save(self) -> None:
        """Persist the current index to disk."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": _CACHE_VERSION,
                "built_at": time.time(),
                "root": str(self._root),
                "entries": [sym.to_dict() for sym in self._entries.values()],
            }
            self._cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            logger.debug("Could not save repomap cache.", exc_info=True)

    def is_fresh(self, rel_path: str, mtime_hash: str) -> bool:
        """Return True if the cached entry for rel_path is still valid."""
        entry = self._entries.get(rel_path)
        return entry is not None and entry.mtime_hash == mtime_hash

    def set(self, sym: FileSymbols) -> None:
        self._entries[sym.path] = sym

    def get(self, rel_path: str) -> FileSymbols | None:
        return self._entries.get(rel_path)

    def remove_stale(self, current_paths: set[str]) -> None:
        """Remove entries for files that no longer exist."""
        stale = set(self._entries) - current_paths
        for p in stale:
            del self._entries[p]

    def all_symbols(self) -> list[FileSymbols]:
        return list(self._entries.values())


# ---------------------------------------------------------------------------
# Public build functions
# ---------------------------------------------------------------------------


def build_repo_map(
    root: Path,
    *,
    extensions: frozenset[str] | None = None,
    max_files: int = _MAX_FILES,
    max_file_size: int = _MAX_FILE_SIZE,
    use_cache: bool = False,
) -> str:
    """Build a structural map of the repository (no caching).

    For most use cases prefer `build_repo_map_cached()` which only re-parses
    changed files.

    Args:
        root: Repository root directory.
        extensions: File extensions to index. Defaults to all supported langs.
        max_files: Maximum number of files to process.
        max_file_size: Skip files larger than this (bytes).
        use_cache: If True, delegates to `build_repo_map_cached()`.

    Returns:
        Formatted repository map string, ready to inject into an LLM prompt.
    """
    if use_cache:
        return build_repo_map_cached(root, extensions=extensions, max_files=max_files, max_file_size=max_file_size)

    exts = extensions or _DEFAULT_EXTENSIONS
    files_parsed = 0
    all_symbols: list[FileSymbols] = []

    for path in sorted(root.rglob("*")):
        if files_parsed >= max_files:
            break

        parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue

        try:
            size = path.stat().st_size
            if size > max_file_size or size == 0:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            rel_path = path.relative_to(root)
            symbols = _extract_symbols(rel_path, content)
            if symbols.classes or symbols.functions:
                all_symbols.append(symbols)
            files_parsed += 1
        except OSError:
            continue

    return _format_map(all_symbols, files_parsed)


def build_repo_map_cached(
    root: Path,
    *,
    extensions: frozenset[str] | None = None,
    max_files: int = _MAX_FILES,
    max_file_size: int = _MAX_FILE_SIZE,
    force_rebuild: bool = False,
) -> str:
    """Build a structural map using incremental file-hash caching.

    Only re-parses files that have changed since the last run. The cache is
    stored in ``<root>/.bog-agents/repomap.json``.

    Args:
        root: Repository root directory.
        extensions: File extensions to index.
        max_files: Maximum number of files to process.
        max_file_size: Skip files larger than this (bytes).
        force_rebuild: Ignore the cache and rebuild from scratch.

    Returns:
        Formatted repository map string.
    """
    exts = extensions or _DEFAULT_EXTENSIONS
    cache = RepoMapCache(root)
    if not force_rebuild:
        cache.load()

    current_paths: set[str] = set()
    files_parsed = 0
    cache_hits = 0

    for path in sorted(root.rglob("*")):
        if files_parsed >= max_files:
            break

        parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue

        try:
            size = path.stat().st_size
            if size > max_file_size or size == 0:
                continue

            rel_path = str(path.relative_to(root))
            mtime_hash = _file_hash(path)
            current_paths.add(rel_path)

            if cache.is_fresh(rel_path, mtime_hash):
                cached = cache.get(rel_path)
                if cached and (cached.classes or cached.functions):
                    cache_hits += 1
                    files_parsed += 1
                    continue

            content = path.read_text(encoding="utf-8", errors="replace")
            symbols = _extract_symbols(path.relative_to(root), content, mtime_hash)
            symbols.size = size
            cache.set(symbols)
            if symbols.classes or symbols.functions:
                files_parsed += 1

        except OSError:
            continue

    cache.remove_stale(current_paths)
    cache.save()

    logger.debug(
        "RepoMap: %d files indexed (%d cache hits, %d re-parsed)",
        files_parsed,
        cache_hits,
        files_parsed - cache_hits,
    )

    all_symbols = [sym for sym in cache.all_symbols() if sym.classes or sym.functions]
    all_symbols.sort(key=lambda s: s.path)
    return _format_map(all_symbols, files_parsed)


def _format_map(all_symbols: list[FileSymbols], files_indexed: int) -> str:
    """Format extracted symbols into an LLM-readable map string."""
    if not all_symbols:
        return "No indexable source files found."

    lines: list[str] = [f"# Repository Map ({files_indexed} files indexed, {len(all_symbols)} with symbols)\n"]
    for sym in all_symbols:
        lines.append(sym.to_summary())
        lines.append("")

    return "\n".join(lines)


def get_repo_map_stats(root: Path) -> dict[str, Any]:
    """Return statistics about the current repo map cache.

    Args:
        root: Repository root directory.

    Returns:
        Dict with keys: cached, file_count, built_at, cache_path.
    """
    cache = RepoMapCache(root)
    cache.load()
    return {
        "cached": len(cache.all_symbols()) > 0,
        "file_count": len(cache.all_symbols()),
        "built_at": cache._built_at,
        "cache_path": str(cache._cache_path),
    }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RepoMapState(TypedDict):
    """LangGraph state shard for repo map middleware."""


class RepoMapMiddleware(AgentMiddleware[RepoMapState, ContextT, ResponseT]):
    """Middleware that builds and injects a repository structural map.

    Provides the LLM with an overview of the codebase architecture
    including file names, class names, function signatures, and type
    definitions — without sending full file contents.

    The map is built incrementally: only changed files are re-parsed,
    using mtime+size fingerprinting. The index is cached at
    ``<working_dir>/.bog-agents/repomap.json``.

    Args:
        working_dir: Repository root directory. Defaults to ``Path.cwd()``.
        extensions: File extensions to index. Defaults to all supported langs.
        max_context_lines: Maximum repo map lines to inject into the system prompt.
            Longer maps are truncated with a note to use the ``repo_map`` tool.
        use_cache: Enable incremental caching (recommended for large repos).
    """

    state_schema = RepoMapState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        extensions: frozenset[str] | None = None,
        max_context_lines: int = 200,
        use_cache: bool = True,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._extensions = extensions
        self._max_context_lines = max_context_lines
        self._use_cache = use_cache
        self._repo_map: str | None = None
        self._tools = self._build_tools()

    @property
    def tools(self) -> list[BaseTool]:
        return self._tools

    def _get_repo_map(self, *, force: bool = False) -> str:
        if self._repo_map is None or force:
            if self._use_cache:
                self._repo_map = build_repo_map_cached(
                    self._working_dir,
                    extensions=self._extensions,
                    force_rebuild=force,
                )
            else:
                self._repo_map = build_repo_map(
                    self._working_dir,
                    extensions=self._extensions,
                )
        return self._repo_map

    def _truncate_for_context(self, repo_map: str) -> str:
        lines = repo_map.splitlines()
        if len(lines) > self._max_context_lines:
            truncated = "\n".join(lines[: self._max_context_lines])
            truncated += (
                f"\n\n... ({len(lines) - self._max_context_lines} more lines truncated. "
                "Use the `repo_map` tool to get the full map or search for specific symbols.)"
            )
            return truncated
        return repo_map

    def _build_tools(self) -> list[BaseTool]:
        middleware = self

        def repo_map_tool(
            runtime: ToolRuntime[None, RepoMapState],
            refresh: bool = False,
        ) -> str:
            """Get the repository structural map (files, classes, functions).

            Shows file names, class definitions, function signatures, and type
            definitions for the entire codebase. Use this to understand the
            project architecture before making changes.

            Pass refresh=True to rebuild the map after you've made significant
            changes to the codebase.
            """
            return middleware._get_repo_map(force=refresh)

        return [
            StructuredTool.from_function(
                name="repo_map",
                description=(
                    "Get a structural map of the repository showing file names, "
                    "class definitions, function signatures, and type definitions. "
                    "Use this to understand the codebase architecture. "
                    "Pass refresh=True to rebuild after making changes."
                ),
                func=repo_map_tool,
            )
        ]

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        repo_map = self._truncate_for_context(self._get_repo_map())
        request = request.override(system_message=append_to_system_message(request.system_message, f"\n\n## Repository Map\n\n{repo_map}"))
        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        repo_map = await asyncio.to_thread(self._get_repo_map)
        repo_map = self._truncate_for_context(repo_map)
        request = request.override(system_message=append_to_system_message(request.system_message, f"\n\n## Repository Map\n\n{repo_map}"))
        return await call_next(request)
