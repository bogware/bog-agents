"""Hybrid codebase search — ripgrep exact + fuzzy filename + optional semantic search.

Combines three search strategies and merges the results with ranked scoring:

1. **Exact** (ripgrep): literal string or regex matches with line numbers.
2. **Fuzzy** (filename): SequenceMatcher against project file names.
3. **Semantic** (embeddings, optional): cosine similarity against cached
   file-chunk embeddings. Requires an embedding model to be configured
   (Ollama ``nomic-embed-text`` by default, or any LangChain Embeddings).

Ranking
-------
``score = exact_weight * exact_hit + semantic_weight * cosine_sim + fuzzy_weight * fuzzy_score``

Default weights: exact=2.0, semantic=1.0, fuzzy=0.5.

Embedding cache
---------------
Stored in ``.bog-agents/embeddings.json`` as a dict mapping relative file paths
to ``{mtime_hash, chunks: [[float, ...]]}`` entries.  Only files whose mtime/
size fingerprint changes are re-embedded.

Usage::

    from bog_agents.middleware.hybrid_search import HybridSearchMiddleware

    agent = create_agent(
        model="claude-opus-4-7",
        middleware=[HybridSearchMiddleware()],
    )

Standalone search (from CLI or @search: mention)::

    from bog_agents.middleware.hybrid_search import hybrid_search

    results = hybrid_search("authentication flow", cwd=Path("/my/project"))
    for r in results:
        print(r.score, r.path, r.snippet)
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.git_env import hardened_git_env

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_VERSION = 1
_CACHE_FILE = ".bog-agents/embeddings.json"
_DEFAULT_EMBED_MODEL = "nomic-embed-text"  # Ollama default
_MAX_RESULTS = 20
_MAX_SNIPPET_CHARS = 300
_CHUNK_SIZE = 40  # lines per embedding chunk
_MAX_FILE_SIZE = 200_000  # bytes — skip large files

_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".git",
        "dist",
        "build",
        "target",
        ".idea",
        ".vs",
        "coverage",
        ".mypy_cache",
        ".ruff_cache",
        ".bog-agents",
    }
)
_EMBED_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".cs",
        ".cpp",
        ".c",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
    }
)

_EXACT_WEIGHT = 2.0
_SEMANTIC_WEIGHT = 1.0
_FUZZY_WEIGHT = 0.5


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """One hit from hybrid_search().

    Attributes:
        path: Relative file path.
        line: Line number (1-indexed), or None for file-level matches.
        snippet: Relevant text excerpt.
        score: Composite relevance score.
        match_type: 'exact', 'semantic', 'fuzzy', or combination.
    """

    path: str
    line: int | None
    snippet: str
    score: float
    match_type: str


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    """Return a fast fingerprint for cache freshness checks.

    Args:
        path: File path.

    Returns:
        String fingerprint using mtime_ns and file size.
    """
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return ""


class EmbeddingCache:
    """Persistent incremental embedding cache stored in .bog-agents/embeddings.json.

    Format::

        {"version": 1, "built_at": 1234567890.0, "entries": {"src/auth.py": {"mtime_hash": "...", "chunks": [[0.12, -0.34, ...], ...]}}}
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache_path = root / _CACHE_FILE
        self._entries: dict[str, dict[str, Any]] = {}
        self._built_at: float = 0.0

    def load(self) -> None:
        """Load the cache from disk."""
        if not self._cache_path.exists():
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if payload.get("version") != _CACHE_VERSION:
                return
            self._entries = payload.get("entries", {})
            self._built_at = float(payload.get("built_at", 0.0))
        except (json.JSONDecodeError, OSError, KeyError):
            self._entries = {}

    def save(self) -> None:
        """Persist the cache to disk.

        Failures (read-only checkout, full disk, Windows path-length limits) are
        swallowed: the cache degrades to in-memory-only rather than discarding all
        embedding compute by raising into the tool caller.
        """
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _CACHE_VERSION,
                "built_at": time.time(),
                "entries": self._entries,
            }
            self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            logger.debug("Could not save embedding cache to %s", self._cache_path, exc_info=True)

    def is_fresh(self, rel_path: str, mtime_hash: str) -> bool:
        """Return True if the cached entry for rel_path is up to date.

        Args:
            rel_path: Relative file path key.
            mtime_hash: Current file fingerprint.

        Returns:
            True if the cache entry is valid and current.
        """
        entry = self._entries.get(rel_path)
        return entry is not None and entry.get("mtime_hash") == mtime_hash

    def set(self, rel_path: str, mtime_hash: str, chunks: list[list[float]]) -> None:
        """Store embeddings for a file.

        Args:
            rel_path: Relative file path key.
            mtime_hash: Current file fingerprint.
            chunks: List of embedding vectors (one per chunk).
        """
        self._entries[rel_path] = {"mtime_hash": mtime_hash, "chunks": chunks}

    def get_chunks(self, rel_path: str) -> list[list[float]]:
        """Return cached embedding chunks for a file.

        Args:
            rel_path: Relative file path key.

        Returns:
            List of embedding vectors, or empty list if not cached.
        """
        entry = self._entries.get(rel_path)
        if entry is None:
            return []
        return entry.get("chunks", [])

    def remove_stale(self, current_paths: set[str]) -> None:
        """Remove entries for files that no longer exist.

        Args:
            current_paths: Set of currently valid relative path keys.
        """
        stale = [k for k in self._entries if k not in current_paths]
        for k in stale:
            del self._entries[k]

    def all_paths(self) -> list[str]:
        """Return all cached file paths."""
        return list(self._entries.keys())


# ---------------------------------------------------------------------------
# Ripgrep exact search
# ---------------------------------------------------------------------------


def _rg_available() -> bool:
    """Return True if ripgrep (rg) is installed.

    Returns:
        True if rg is on PATH.
    """
    return shutil.which("rg") is not None


def ripgrep_search(
    query: str,
    cwd: Path,
    *,
    max_results: int = _MAX_RESULTS,
    case_insensitive: bool = True,
    fixed_strings: bool = True,
) -> list[SearchResult]:
    """Run ripgrep and return structured results.

    Args:
        query: Search term or regex pattern.
        cwd: Directory to search in.
        max_results: Maximum number of results.
        case_insensitive: Use case-insensitive matching.
        fixed_strings: Treat query as literal string (not regex).

    Returns:
        List of SearchResult with exact match_type.
    """
    if not _rg_available():
        logger.debug("ripgrep (rg) not found — skipping exact search")
        return []

    cmd = ["rg", "--json", f"--max-count={max_results * 2}"]
    if case_insensitive:
        cmd.append("--ignore-case")
    if fixed_strings:
        cmd.append("--fixed-strings")
    # Skip common non-code dirs
    for skip in _SKIP_DIRS:
        cmd.extend(["--glob", f"!{skip}/**"])
    cmd.append(query)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    results: list[SearchResult] = []
    seen_paths: set[str] = set()

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        rel_path = data.get("path", {}).get("text", "")
        line_num = data.get("line_number")
        lines_obj = data.get("lines", {})
        text = lines_obj.get("text", "").strip()[:_MAX_SNIPPET_CHARS]

        results.append(
            SearchResult(
                path=rel_path,
                line=line_num,
                snippet=text,
                score=_EXACT_WEIGHT,
                match_type="exact",
            )
        )
        seen_paths.add(rel_path)
        if len(results) >= max_results:
            break

    return results


# ---------------------------------------------------------------------------
# Fuzzy filename search
# ---------------------------------------------------------------------------


def _get_project_files(root: Path) -> list[str]:
    """Return relative file paths for indexable files.

    Args:
        root: Project root directory.

    Returns:
        List of relative POSIX file paths.
    """
    files: list[str] = []
    try:
        # Try git ls-files first (fast and respects .gitignore)
        if shutil.which("git"):
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env=hardened_git_env(),
            )
            if result.returncode == 0:
                return [f for f in result.stdout.strip().split("\n") if f]
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: walk the directory tree
    for p in root.rglob("*"):
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
            try:
                files.append(p.relative_to(root).as_posix())
            except ValueError:
                pass
    return files


def fuzzy_file_search(
    query: str,
    files: list[str],
    *,
    max_results: int = _MAX_RESULTS,
) -> list[SearchResult]:
    """Score files by name similarity to query.

    Args:
        query: Search query.
        files: List of relative file paths.
        max_results: Maximum results to return.

    Returns:
        List of SearchResult with fuzzy match_type.
    """
    query_lower = query.lower()
    scored: list[tuple[float, str]] = []

    for path in files:
        filename = path.rsplit("/", 1)[-1].lower()
        path_lower = path.lower()

        # Exact filename or path substring → high score
        if query_lower in filename:
            idx = filename.find(query_lower)
            score = 0.9 + (0.1 if idx == 0 else 0.0)
        elif query_lower in path_lower:
            score = 0.6
        else:
            ratio = SequenceMatcher(None, query_lower, filename).ratio()
            if ratio < 0.4:
                continue
            score = ratio * 0.5

        scored.append((_FUZZY_WEIGHT * score, path))

    scored.sort(reverse=True)
    return [SearchResult(path=p, line=None, snippet=f"File: {p}", score=s, match_type="fuzzy") for s, p in scored[:max_results]]


# ---------------------------------------------------------------------------
# Cosine similarity (pure Python)
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity in [-1, 1].
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Semantic search (optional — requires embedding model)
# ---------------------------------------------------------------------------


def _get_embedding_model(model_name: str = _DEFAULT_EMBED_MODEL) -> Any | None:
    """Return a LangChain Embeddings instance, or None if unavailable.

    Tries Ollama first (local, free), then falls back gracefully.

    Args:
        model_name: Embedding model name.

    Returns:
        LangChain Embeddings instance or None.
    """
    try:
        from langchain_ollama import OllamaEmbeddings  # type: ignore[import]

        return OllamaEmbeddings(model=model_name)
    except ImportError:
        pass
    try:
        from langchain_openai import OpenAIEmbeddings  # type: ignore[import]

        return OpenAIEmbeddings()
    except ImportError:
        pass
    return None


def _chunk_file(path: Path, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """Split a file into overlapping text chunks.

    Args:
        path: File to chunk.
        chunk_size: Lines per chunk.

    Returns:
        List of text chunks.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if not lines:
        return []
    chunks = []
    step = max(1, chunk_size // 2)  # 50% overlap
    for i in range(0, len(lines), step):
        chunk = "\n".join(lines[i : i + chunk_size])
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def build_embedding_index(
    root: Path,
    *,
    embed_model: Any | None = None,
    force_rebuild: bool = False,
) -> EmbeddingCache:
    """Build or update the embedding index for a project.

    Only files whose mtime/size fingerprint has changed are re-embedded.

    Args:
        root: Project root directory.
        embed_model: LangChain Embeddings instance. If None, tries auto-detect.
        force_rebuild: Discard the cache and rebuild from scratch.

    Returns:
        Loaded EmbeddingCache (may be empty if no embedding model is available).
    """
    model = embed_model or _get_embedding_model()
    cache = EmbeddingCache(root)

    if not force_rebuild:
        cache.load()

    if model is None:
        logger.debug("No embedding model available — semantic search disabled")
        return cache

    files = _get_project_files(root)
    current_paths: set[str] = set()
    updated = 0

    for rel_path in files:
        full_path = root / rel_path
        if full_path.suffix.lower() not in _EMBED_EXTENSIONS:
            continue
        try:
            if full_path.stat().st_size > _MAX_FILE_SIZE:
                continue
        except OSError:
            continue

        mtime_hash = _file_hash(full_path)
        current_paths.add(rel_path)

        if cache.is_fresh(rel_path, mtime_hash):
            continue

        chunks = _chunk_file(full_path)
        if not chunks:
            continue

        try:
            vectors = model.embed_documents(chunks)
            cache.set(rel_path, mtime_hash, vectors)
            updated += 1
        except Exception as exc:
            logger.debug("Failed to embed %s: %s", rel_path, exc)

    cache.remove_stale(current_paths)

    if updated > 0:
        cache.save()
        logger.debug("Updated embeddings for %d files", updated)

    return cache


def semantic_search(
    query: str,
    cache: EmbeddingCache,
    *,
    embed_model: Any | None = None,
    max_results: int = _MAX_RESULTS,
) -> list[SearchResult]:
    """Semantic similarity search against the embedding cache.

    Args:
        query: Natural language query.
        cache: Preloaded embedding cache.
        embed_model: LangChain Embeddings instance. If None, tries auto-detect.
        max_results: Maximum results to return.

    Returns:
        List of SearchResult with semantic match_type.
    """
    model = embed_model or _get_embedding_model()
    if model is None:
        return []

    try:
        query_vec = model.embed_query(query)
    except Exception as exc:
        logger.debug("Failed to embed query: %s", exc)
        return []

    scored: list[tuple[float, str]] = []
    for rel_path in cache.all_paths():
        chunks = cache.get_chunks(rel_path)
        if not chunks:
            continue
        best_sim = max(_cosine_similarity(query_vec, chunk) for chunk in chunks)
        if best_sim > 0.3:
            scored.append((_SEMANTIC_WEIGHT * best_sim, rel_path))

    scored.sort(reverse=True)
    return [SearchResult(path=p, line=None, snippet=f"Semantic match: {p}", score=s, match_type="semantic") for s, p in scored[:max_results]]


# ---------------------------------------------------------------------------
# Hybrid ranking
# ---------------------------------------------------------------------------


def hybrid_search(
    query: str,
    cwd: Path,
    *,
    embed_model: Any | None = None,
    max_results: int = _MAX_RESULTS,
    use_semantic: bool = True,
    use_exact: bool = True,
    use_fuzzy: bool = True,
) -> list[SearchResult]:
    """Perform hybrid codebase search: exact + fuzzy + semantic.

    Results are deduplicated by path and ranked by combined score.

    Args:
        query: Search query (natural language or keyword).
        cwd: Project root directory.
        embed_model: LangChain Embeddings instance. Auto-detected if None.
        max_results: Maximum results to return.
        use_semantic: Include semantic search (requires embedding model).
        use_exact: Include ripgrep exact search.
        use_fuzzy: Include fuzzy filename search.

    Returns:
        Ranked list of SearchResult objects.
    """
    all_results: list[SearchResult] = []

    # 1. Exact search via ripgrep
    if use_exact:
        all_results.extend(ripgrep_search(query, cwd, max_results=max_results * 2))

    # 2. Fuzzy filename search
    if use_fuzzy:
        files = _get_project_files(cwd)
        all_results.extend(fuzzy_file_search(query, files, max_results=max_results))

    # 3. Semantic search (optional)
    if use_semantic:
        try:
            cache = EmbeddingCache(cwd)
            cache.load()
            if cache.all_paths():
                all_results.extend(semantic_search(query, cache, embed_model=embed_model, max_results=max_results))
        except Exception as exc:
            logger.debug("Semantic search failed: %s", exc)

    # Merge: sum scores for the same path, keep best snippet
    merged: dict[str, SearchResult] = {}
    for result in all_results:
        key = f"{result.path}:{result.line or ''}"
        if key in merged:
            existing = merged[key]
            existing.score += result.score
            existing.match_type = f"{existing.match_type}+{result.match_type}"
        else:
            merged[key] = result

    ranked = sorted(merged.values(), key=lambda r: -r.score)
    return ranked[:max_results]


def format_search_results(results: list[SearchResult]) -> str:
    """Format search results for display.

    Args:
        results: List of SearchResult objects.

    Returns:
        Human-readable formatted string.
    """
    if not results:
        return "No results found."
    lines = [f"Found {len(results)} result(s):\n"]
    for i, r in enumerate(results, 1):
        loc = f":{r.line}" if r.line else ""
        lines.append(f"{i}. [{r.match_type}] {r.path}{loc}  (score: {r.score:.2f})")
        if r.snippet:
            lines.append(f"   {r.snippet[:120]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------


class HybridSearchState(TypedDict):
    """State for the hybrid search middleware."""


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class HybridSearchMiddleware(AgentMiddleware[HybridSearchState, ContextT, ResponseT]):
    """Expose hybrid codebase search as an agent tool.

    Combines ripgrep exact search, fuzzy filename matching, and optional
    semantic (embedding) search into a single ranked result set.

    Args:
        working_dir: Project root directory.
        embed_model: LangChain Embeddings instance. If None, tries Ollama then
            OpenAI. Semantic search is silently disabled if unavailable.
        max_results: Maximum results returned per search.
    """

    state_schema = HybridSearchState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        embed_model: Any | None = None,
        max_results: int = 10,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._embed_model = embed_model
        self._max_results = max_results
        self._tools = self._build_tools()

    @property
    def tools(self) -> list[BaseTool]:
        """Expose the search_codebase tool."""
        return self._tools

    def _rebuild_index(self, *, force: bool = False) -> str:
        """Build or refresh the embedding index synchronously.

        Args:
            force: If True, discard existing cache and rebuild from scratch.

        Returns:
            Human-readable summary string.
        """
        model = self._embed_model or _get_embedding_model()
        if model is None:
            return "No embedding model available.\nInstall Ollama and run: ollama pull nomic-embed-text\nOr set OPENAI_API_KEY for OpenAI embeddings."
        import time as _time

        start = _time.monotonic()
        cache = build_embedding_index(self._working_dir, embed_model=model, force_rebuild=force)
        elapsed = _time.monotonic() - start
        count = len(cache.all_paths())
        return f"Indexed {count} files in {elapsed:.1f}s."

    def _build_tools(self) -> list[BaseTool]:
        """Build the search tools."""
        mw = self

        def search_codebase(
            runtime: ToolRuntime[None, HybridSearchState],
            query: Annotated[str, "Search query — natural language or keyword"],
            use_semantic: Annotated[bool, "Include semantic embedding search (slower)"] = True,
        ) -> str:
            """Search the codebase using hybrid exact + fuzzy + semantic search.

            Returns ranked results from all available search strategies.
            """
            results = hybrid_search(
                query,
                mw._working_dir,
                embed_model=mw._embed_model,
                max_results=mw._max_results,
                use_semantic=use_semantic,
            )
            return format_search_results(results)

        def index_codebase(
            runtime: ToolRuntime[None, HybridSearchState],
            force_rebuild: Annotated[bool, "Discard existing cache and rebuild from scratch"] = False,
        ) -> str:
            """Build or update the semantic search index for this project.

            This is required before semantic search works. Ripgrep and fuzzy
            search work immediately without indexing.
            """
            model = mw._embed_model or _get_embedding_model()
            if model is None:
                return (
                    "No embedding model available.\n"
                    "Install Ollama (https://ollama.com) and run: ollama pull nomic-embed-text\n"
                    "Or set OPENAI_API_KEY for OpenAI embeddings."
                )

            start = time.monotonic()
            cache = build_embedding_index(mw._working_dir, embed_model=model, force_rebuild=force_rebuild)
            elapsed = time.monotonic() - start
            count = len(cache.all_paths())
            return f"Indexed {count} files in {elapsed:.1f}s. Semantic search is now available."

        return [
            StructuredTool.from_function(
                name="search_codebase",
                description="Hybrid codebase search: exact (ripgrep) + fuzzy + semantic.",
                func=search_codebase,
            ),
            StructuredTool.from_function(
                name="index_codebase",
                description="Build or refresh the semantic search index for this project.",
                func=index_codebase,
            ),
        ]
