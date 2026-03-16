"""Middleware for repository map / codebase indexing.

Feature #13: Builds a structural map of the codebase including file names,
class/function signatures, and import relationships. Provides the LLM
with architectural understanding without sending full file contents.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
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

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

# Language-specific regex patterns for extracting symbols
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
        ("type", re.compile(r"^(?:export\s+)?type\s+(\w+)", re.MULTILINE)),
        ("import", re.compile(r"^import\s+.+\s+from\s+['\"](.+?)['\"]", re.MULTILINE)),
    ],
    ".rs": [
        ("struct", re.compile(r"^pub\s+struct\s+(\w+)", re.MULTILINE)),
        ("enum", re.compile(r"^pub\s+enum\s+(\w+)", re.MULTILINE)),
        ("fn", re.compile(r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)),
        ("trait", re.compile(r"^pub\s+trait\s+(\w+)", re.MULTILINE)),
        ("impl", re.compile(r"^impl(?:<.*?>)?\s+(\w+)", re.MULTILINE)),
        ("use", re.compile(r"^use\s+(.+);", re.MULTILINE)),
    ],
    ".go": [
        ("struct", re.compile(r"^type\s+(\w+)\s+struct", re.MULTILINE)),
        ("interface", re.compile(r"^type\s+(\w+)\s+interface", re.MULTILINE)),
        ("func", re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(", re.MULTILINE)),
        ("import", re.compile(r"\"(.+?)\"", re.MULTILINE)),
    ],
    ".java": [
        ("class", re.compile(r"^(?:public\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)),
        ("interface", re.compile(r"^(?:public\s+)?interface\s+(\w+)", re.MULTILINE)),
        ("method", re.compile(r"^\s+(?:public|private|protected)\s+\S+\s+(\w+)\s*\(", re.MULTILINE)),
        ("import", re.compile(r"^import\s+(.+);", re.MULTILINE)),
    ],
}

# Map additional extensions to their pattern set
_EXT_MAP: dict[str, str] = {
    ".tsx": ".ts",
    ".jsx": ".js",
    ".mjs": ".js",
    ".cjs": ".js",
    ".mts": ".ts",
    ".cts": ".ts",
}

# Default extensions to index
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
    }
)

# Max file size to parse (skip very large files)
_MAX_FILE_SIZE = 500_000  # 500KB

# Max files to index
_MAX_FILES = 5000


class FileSymbols:
    """Extracted symbols from a single file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.imports: list[str] = []
        self.other: dict[str, list[str]] = defaultdict(list)

    def to_summary(self) -> str:
        """Generate a concise summary line for this file.

        Returns:
            Summary string.
        """
        parts = [self.path]
        if self.classes:
            parts.append(f"  classes: {', '.join(self.classes)}")
        if self.functions:
            # Show up to 10 functions to avoid overwhelming output
            fns = self.functions[:10]
            suffix = f" (+{len(self.functions) - 10} more)" if len(self.functions) > 10 else ""
            parts.append(f"  functions: {', '.join(fns)}{suffix}")
        for category, items in self.other.items():
            if items:
                shown = items[:5]
                suffix = f" (+{len(items) - 5} more)" if len(items) > 5 else ""
                parts.append(f"  {category}: {', '.join(shown)}{suffix}")
        return "\n".join(parts)


def _extract_symbols(path: Path, content: str) -> FileSymbols:
    """Extract symbols from a file based on its extension.

    Args:
        path: File path.
        content: File content.

    Returns:
        FileSymbols with extracted classes, functions, imports.
    """
    symbols = FileSymbols(str(path))
    ext = path.suffix.lower()
    patterns_key = _EXT_MAP.get(ext, ext)
    patterns = _PATTERNS.get(patterns_key, [])

    for category, pattern in patterns:
        for match in pattern.finditer(content):
            name = match.group(1)
            if not name:
                continue
            if category in ("class", "struct", "enum", "interface", "type", "trait"):
                symbols.classes.append(name)
            elif category in ("function", "fn", "func", "method", "const_fn"):
                symbols.functions.append(name)
            elif category in ("import", "use"):
                symbols.imports.append(name)
            elif category == "impl":
                symbols.other["impl"].append(name)

    return symbols


def build_repo_map(
    root: Path,
    *,
    extensions: frozenset[str] | None = None,
    max_files: int = _MAX_FILES,
    max_file_size: int = _MAX_FILE_SIZE,
) -> str:
    """Build a structural map of the repository.

    Args:
        root: Repository root directory.
        extensions: File extensions to index.
        max_files: Maximum number of files to process.
        max_file_size: Maximum file size in bytes.

    Returns:
        Formatted repository map string.
    """
    exts = extensions or _DEFAULT_EXTENSIONS
    files_found = 0
    all_symbols: list[FileSymbols] = []

    for path in sorted(root.rglob("*")):
        if files_found >= max_files:
            break

        # Skip hidden directories and common non-source directories
        parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in ("node_modules", "__pycache__", "venv", ".venv", "dist", "build", "target") for p in parts):
            continue

        if not path.is_file():
            continue

        if path.suffix.lower() not in exts:
            continue

        try:
            size = path.stat().st_size
            if size > max_file_size or size == 0:
                continue

            content = path.read_text(errors="replace")
            rel_path = path.relative_to(root)
            symbols = _extract_symbols(rel_path, content)
            if symbols.classes or symbols.functions:
                all_symbols.append(symbols)
            files_found += 1
        except OSError:
            continue

    if not all_symbols:
        return "No indexable source files found."

    lines = [f"# Repository Map ({files_found} files indexed)\n"]
    for sym in all_symbols:
        lines.append(sym.to_summary())
        lines.append("")

    return "\n".join(lines)


class RepoMapState(TypedDict):
    """State for the repo map middleware."""


class RepoMapMiddleware(AgentMiddleware[RepoMapState, ContextT, ResponseT]):
    """Middleware that builds and injects a repository structural map.

    Provides the LLM with an overview of the codebase architecture
    including file names, class names, function signatures, and imports,
    without sending full file contents.

    Args:
        working_dir: Repository root directory.
        extensions: File extensions to index.
        max_context_lines: Maximum lines of repo map to inject into context.
    """

    state_schema = RepoMapState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        extensions: frozenset[str] | None = None,
        max_context_lines: int = 200,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._extensions = extensions
        self._max_context_lines = max_context_lines
        self._repo_map: str | None = None
        self._tools = self._build_tools()

    @property
    def tools(self) -> list[BaseTool]:
        """Tools provided by this middleware."""
        return self._tools

    def _get_repo_map(self) -> str:
        """Get or build the repository map.

        Returns:
            Repository map string.
        """
        if self._repo_map is None:
            self._repo_map = build_repo_map(self._working_dir, extensions=self._extensions)
        return self._repo_map

    def _build_tools(self) -> list[BaseTool]:
        """Build the repo map tools."""
        middleware = self

        def repo_map_tool(
            runtime: ToolRuntime[None, RepoMapState],
            refresh: bool = False,
        ) -> str:
            """Get the repository structural map showing files, classes, and functions. Use refresh=True to rebuild."""
            if refresh:
                middleware._repo_map = None
            return middleware._get_repo_map()

        return [
            StructuredTool.from_function(
                name="repo_map",
                description=(
                    "Get a structural map of the repository showing file names, "
                    "class definitions, function signatures, and imports. "
                    "Use this to understand the codebase architecture before making changes. "
                    "Pass refresh=True to rebuild the map after changes."
                ),
                func=repo_map_tool,
            )
        ]

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject a compact repo map summary into the system prompt.

        Args:
            request: The model request.
            call_next: Next handler.

        Returns:
            Model response.
        """
        repo_map = self._get_repo_map()
        lines = repo_map.splitlines()
        if len(lines) > self._max_context_lines:
            truncated = "\n".join(lines[: self._max_context_lines])
            truncated += f"\n\n... ({len(lines) - self._max_context_lines} more lines, use repo_map tool for full map)"
            repo_map = truncated

        context = f"\n\n## Repository Map\n\n{repo_map}"
        request = append_to_system_message(request, context)
        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        repo_map = await asyncio.to_thread(self._get_repo_map)
        lines = repo_map.splitlines()
        if len(lines) > self._max_context_lines:
            truncated = "\n".join(lines[: self._max_context_lines])
            truncated += f"\n\n... ({len(lines) - self._max_context_lines} more lines, use repo_map tool for full map)"
            repo_map = truncated

        context = f"\n\n## Repository Map\n\n{repo_map}"
        request = append_to_system_message(request, context)
        return await call_next(request)
