"""Lightweight codebase indexer — no embeddings required.

Uses file content + TF-IDF-style scoring for fast, local-only search.
"""

from __future__ import annotations

import hashlib
import json
import re

# S404: subprocess is used for git ls-files and rg/grep; inputs are hardcoded or validated
import subprocess  # noqa: S404
from datetime import UTC, datetime
from pathlib import Path

_INDEX_BASE: Path = Path.home() / ".bog-agents" / "index"
_MAX_FILE_SIZE = 500 * 1024  # 500 KB
_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:def |class |function |const |let |var |export (?:default )?(?:function |class )?)"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _project_hash(cwd: Path) -> str:
    """Return an 8-char hex hash of the cwd path.

    Args:
        cwd: Absolute path to the project root.

    Returns:
        8-character hexadecimal hash string.
    """
    return hashlib.md5(str(cwd).encode()).hexdigest()[:8]  # noqa: S324


def _index_path(cwd: Path) -> Path:
    """Return the path to the index file for the given project.

    Args:
        cwd: Absolute path to the project root.

    Returns:
        Path to the index JSON file.
    """
    return _INDEX_BASE / _project_hash(cwd) / "index.json"


def _list_tracked_files(cwd: Path) -> list[str]:
    """List files tracked by git, falling back to a recursive glob.

    Args:
        cwd: Project root directory.

    Returns:
        List of relative file paths.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback: glob for text-ish files, limited depth
    files: list[str] = []
    try:
        for p in cwd.rglob("*"):
            if not p.is_file():
                continue
            # Skip hidden dirs/files
            if any(part.startswith(".") for part in p.relative_to(cwd).parts):
                continue
            files.append(p.relative_to(cwd).as_posix())
    except OSError:
        pass
    return files


def _extract_symbols(file_path: Path) -> list[str]:
    """Extract top-level symbol names from a source file.

    Uses rg when available; falls back to grep; falls back to regex on content.

    Args:
        file_path: Absolute path to the file.

    Returns:
        List of symbol names found in the file.
    """
    symbols: list[str] = []

    # Try rg first (fast)
    try:
        result = subprocess.run(  # noqa: S603
            [
                "rg",
                "--no-filename",
                "--only-matching",
                r"(?:def |class |function |const |let |var )\K\w+",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return [s for s in result.stdout.strip().split("\n") if s]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Try grep fallback
    try:
        result = subprocess.run(  # noqa: S603
            [
                "grep",
                "-oE",
                r"(def |class |function |const |let |var )[A-Za-z_][A-Za-z0-9_]*",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    symbols.append(parts[1])
            return symbols
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Pure-Python fallback: read and regex
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        for m in _SYMBOL_PATTERN.finditer(text):
            symbols.append(m.group(1))
    except OSError:
        pass

    return symbols


def _is_binary_or_generated(rel_path: str) -> bool:
    """Return True if the file should be skipped.

    Args:
        rel_path: Relative path string.

    Returns:
        True if the file appears to be binary or auto-generated.
    """
    skip_exts = {
        ".pyc",
        ".pyo",
        ".so",
        ".o",
        ".a",
        ".dylib",
        ".dll",
        ".exe",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".whl",
        ".egg",
        ".lock",  # lockfiles can be huge and are rarely searched
        ".min.js",
        ".min.css",
        ".map",
    }
    # Check full extension combinations (e.g. .min.js)
    lower = rel_path.lower()
    return any(lower.endswith(ext) for ext in skip_exts)


def build_index(cwd: Path, *, force: bool = False) -> str:
    """Build or rebuild the codebase index.

    Indexes all text files in cwd (respecting .gitignore via git ls-files).
    Stores index at ~/.bog-agents/index/<project_hash>/index.json.

    Index format::

        {
            "created_at": ISO datetime,
            "root": str(cwd),
            "files": {
                "relative/path.py": {
                    "symbols": ["MyClass", "my_function", ...],
                    "summary": "first 3 lines of file",
                    "size": int,
                }
            }
        }

    Args:
        cwd: Project root to index.
        force: If True, rebuild even if an index already exists.

    Returns:
        Rich-formatted progress/result string.
    """
    idx_path = _index_path(cwd)

    if not force and idx_path.exists():
        try:
            existing = json.loads(idx_path.read_text(encoding="utf-8"))
            created = existing.get("created_at", "unknown")
            count = len(existing.get("files", {}))
            return f"[green]Index already exists[/green] (built {created}, {count} files). Use `force=True` or `/index rebuild` to refresh."
        except (OSError, json.JSONDecodeError):
            pass

    idx_path.parent.mkdir(parents=True, exist_ok=True)

    all_files = _list_tracked_files(cwd)
    files_data: dict[str, dict] = {}
    skipped = 0

    for rel in all_files:
        if _is_binary_or_generated(rel):
            skipped += 1
            continue

        abs_path = cwd / rel
        try:
            size = abs_path.stat().st_size
        except OSError:
            skipped += 1
            continue

        if size > _MAX_FILE_SIZE:
            skipped += 1
            continue

        # Extract summary (first 3 non-empty lines)
        summary_lines: list[str] = []
        try:
            with abs_path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        summary_lines.append(stripped)
                    if len(summary_lines) >= 3:
                        break
        except OSError:
            pass

        symbols = _extract_symbols(abs_path)
        files_data[rel] = {
            "symbols": symbols,
            "summary": " | ".join(summary_lines),
            "size": size,
        }

    index = {
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(cwd),
        "files": files_data,
    }

    try:
        idx_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    except OSError as exc:
        return f"[red]Failed to write index:[/red] {exc}"

    total = len(files_data)
    return (
        f"[green]Index built[/green]: {total} files indexed, {skipped} skipped "
        f"(binary/large/generated). Stored at [dim]{idx_path}[/dim]"
    )


def _load_index(cwd: Path) -> dict | None:
    """Load the index from disk, returning None on any failure.

    Args:
        cwd: Project root whose index to load.

    Returns:
        Parsed index dict, or None if unavailable.
    """
    idx_path = _index_path(cwd)
    if not idx_path.exists():
        return None
    try:
        return json.loads(idx_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _tfidf_score(query: str, file_entry: dict, rel_path: str) -> float:
    """Compute a relevance score for a query against a file index entry.

    Args:
        query: Search query string.
        file_entry: Dict with ``symbols``, ``summary``, and ``size`` keys.
        rel_path: Relative file path.

    Returns:
        Float relevance score; 0 means no match.
    """
    q = query.lower()
    score = 0.0

    # Symbol name match (highest weight)
    for sym in file_entry.get("symbols", []):
        sym_lower = sym.lower()
        if sym_lower == q:
            score += 100
        elif sym_lower.startswith(q):
            score += 60
        elif q in sym_lower:
            score += 30

    # File path / name match
    path_lower = rel_path.lower()
    filename = rel_path.rsplit("/", 1)[-1].lower()
    if filename == q or filename.startswith(q + "."):
        score += 80
    elif q in filename:
        score += 40
    elif q in path_lower:
        score += 20

    # Summary match (lower weight)
    summary_lower = file_entry.get("summary", "").lower()
    if q in summary_lower:
        score += 10

    return score


def search_index(query: str, cwd: Path, *, limit: int = 10) -> str:
    """Search the codebase index.

    Searches:

    1. Symbol names (function/class definitions matching query)
    2. File names matching query
    3. File content summary

    Args:
        query: Search term.
        cwd: Project root whose index to search.
        limit: Maximum number of results to return.

    Returns:
        Rich-formatted table with columns: file | symbol/context | score.
    """
    if not query.strip():
        return "[yellow]Please provide a search query.[/yellow]"

    index = _load_index(cwd)
    if index is None:
        return "[yellow]No index found.[/yellow] Run [bold]/index build[/bold] first."

    files = index.get("files", {})
    scored: list[tuple[float, str, str]] = []  # (score, rel_path, context)

    for rel_path, entry in files.items():
        score = _tfidf_score(query, entry, rel_path)
        if score <= 0:
            continue

        # Build context string: matching symbols or summary excerpt
        q_lower = query.lower()
        matching_syms = [s for s in entry.get("symbols", []) if q_lower in s.lower()]
        if matching_syms:
            context = ", ".join(matching_syms[:5])
        else:
            summary = entry.get("summary", "")
            context = summary[:80] + ("…" if len(summary) > 80 else "")

        scored.append((score, rel_path, context))

    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]

    if not top:
        return f"[yellow]No results for[/yellow] [bold]{query!r}[/bold]."

    lines = [f"[bold]Search results for[/bold] [green]{query!r}[/green]:\n"]
    lines.append(f"{'File':<50} {'Context':<50} {'Score':>6}")
    lines.append("-" * 110)
    for score, rel_path, context in top:
        lines.append(f"{rel_path:<50} {context:<50} {score:>6.0f}")

    return "\n".join(lines)


def index_status(cwd: Path) -> str:
    """Show index status: last built, file count, size.

    Args:
        cwd: Project root whose index to inspect.

    Returns:
        Rich-formatted status string.
    """
    idx_path = _index_path(cwd)
    if not idx_path.exists():
        return "[yellow]No index found.[/yellow] Run [bold]/index build[/bold] to create one."

    index = _load_index(cwd)
    if index is None:
        return "[red]Index file is corrupt.[/red] Run [bold]/index rebuild[/bold]."

    created_at = index.get("created_at", "unknown")
    root = index.get("root", str(cwd))
    file_count = len(index.get("files", {}))
    try:
        size_bytes = idx_path.stat().st_size
        size_str = (
            f"{size_bytes / 1024:.1f} KB" if size_bytes >= 1024 else f"{size_bytes} B"
        )
    except OSError:
        size_str = "unknown"

    return (
        f"[bold]Index status[/bold]\n"
        f"  Root     : {root}\n"
        f"  Built    : {created_at}\n"
        f"  Files    : {file_count}\n"
        f"  Disk size: {size_str}\n"
        f"  Location : [dim]{idx_path}[/dim]"
    )


def format_index_help() -> str:
    """Return usage help for /index command.

    Returns:
        Formatted help string describing /index sub-commands.
    """
    return (
        "[bold]/index[/bold] — lightweight codebase indexer\n\n"
        "  [bold]/index build[/bold]          Build the index for the current project\n"
        "  [bold]/index rebuild[/bold]        Force rebuild (overwrite existing index)\n"
        "  [bold]/index search <query>[/bold] Search the index for symbols or files\n"
        "  [bold]/index status[/bold]         Show index metadata (built time, file count)\n"
        "  [bold]/index help[/bold]           Show this help\n\n"
        "The index is stored at [dim]~/.bog-agents/index/<project_hash>/index.json[/dim].\n"
        "Only tracked files (via `git ls-files`) are indexed, so .gitignore is respected."
    )
