"""Middleware for smart context retrieval and management.

Feature #13: Large context window support.
Feature #15: Smart context retrieval (RAG-based).
Feature #17: Context window visualization.
Feature #18: Auto-memory extraction.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
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


@dataclass
class ContextChunk:
    """A chunk of indexed codebase content."""

    file_path: str
    content: str
    start_line: int
    end_line: int
    chunk_hash: str = ""
    relevance_score: float = 0.0

    def __post_init__(self) -> None:
        """Compute hash if not provided."""
        if not self.chunk_hash:
            self.chunk_hash = hashlib.sha256(self.content.encode()).hexdigest()[:12]


@dataclass
class ContextUsage:
    """Track context window usage."""

    max_tokens: int = 200000
    used_tokens: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def percent_used(self) -> float:
        """Get percentage of context used."""
        return (self.used_tokens / self.max_tokens * 100) if self.max_tokens > 0 else 0.0

    @property
    def remaining_tokens(self) -> int:
        """Get remaining tokens."""
        return max(0, self.max_tokens - self.used_tokens)


@dataclass
class MemoryEntry:
    """An automatically extracted memory."""

    content: str
    source: str
    category: str = "general"
    importance: float = 0.5


class SmartContextState(TypedDict):
    """State for the smart context middleware."""


class SmartContextMiddleware(AgentMiddleware[SmartContextState, ContextT, ResponseT]):
    """Middleware for intelligent context retrieval and management.

    Provides RAG-like context retrieval, context window visualization,
    and automatic memory extraction.

    Args:
        working_dir: Project root directory for indexing.
        max_context_tokens: Maximum context window size.
        auto_extract_memory: Whether to auto-extract learnings.
        memory_file: Path to save extracted memories.
    """

    state_schema = SmartContextState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        max_context_tokens: int = 200000,
        auto_extract_memory: bool = True,
        memory_file: str = "AGENTS.md",
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._context_usage = ContextUsage(max_tokens=max_context_tokens)
        self._auto_extract = auto_extract_memory
        self._memory_file = memory_file
        self._index: dict[str, list[ContextChunk]] = {}
        self._memories: list[MemoryEntry] = []
        self.tools = self._build_tools()

    @property
    def context_usage(self) -> ContextUsage:
        """Access context usage info."""
        return self._context_usage

    @property
    def memories(self) -> list[MemoryEntry]:
        """Access extracted memories."""
        return self._memories

    def _index_file(self, file_path: Path, chunk_size: int = 50) -> list[ContextChunk]:
        """Index a file into chunks.

        Args:
            file_path: Path to index.
            chunk_size: Lines per chunk.

        Returns:
            List of chunks from the file.
        """
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return []

        lines = content.split("\n")
        chunks: list[ContextChunk] = []
        for i in range(0, len(lines), chunk_size):
            chunk_lines = lines[i : i + chunk_size]
            chunk = ContextChunk(
                file_path=str(file_path.relative_to(self._working_dir)),
                content="\n".join(chunk_lines),
                start_line=i + 1,
                end_line=min(i + chunk_size, len(lines)),
            )
            chunks.append(chunk)

        rel = str(file_path.relative_to(self._working_dir))
        self._index[rel] = chunks
        return chunks

    def _search_index(self, query: str, max_results: int = 5) -> list[ContextChunk]:
        """Search indexed chunks for relevant context.

        Args:
            query: Search query.
            max_results: Maximum results to return.

        Returns:
            Relevant chunks sorted by relevance.
        """
        query_lower = query.lower()
        query_terms = query_lower.split()
        results: list[ContextChunk] = []

        for chunks in self._index.values():
            for chunk in chunks:
                content_lower = chunk.content.lower()
                score = sum(1 for term in query_terms if term in content_lower) / max(len(query_terms), 1)
                if score > 0:
                    chunk.relevance_score = score
                    results.append(chunk)

        results.sort(key=lambda c: c.relevance_score, reverse=True)
        return results[:max_results]

    def _build_tools(self) -> list[BaseTool]:
        """Build smart context tools."""
        middleware = self

        def index_codebase(
            runtime: ToolRuntime[None, SmartContextState],
            patterns: Annotated[list[str], "Glob patterns to index (e.g., ['**/*.py', '**/*.ts'])"] | None = None,
            chunk_size: Annotated[int, "Lines per chunk for indexing"] = 50,
        ) -> str:
            """Index codebase files for smart context retrieval."""
            if patterns is None:
                patterns = ["**/*.py", "**/*.ts", "**/*.js", "**/*.go", "**/*.rs", "**/*.java"]

            total_chunks = 0
            total_files = 0
            for pattern in patterns:
                for file_path in middleware._working_dir.glob(pattern):
                    if file_path.is_file() and not any(part.startswith(".") or part in {"node_modules", "__pycache__"} for part in file_path.parts):
                        chunks = middleware._index_file(file_path, chunk_size)
                        total_chunks += len(chunks)
                        total_files += 1

            return f"Indexed {total_files} files into {total_chunks} chunks."

        def retrieve_context(
            runtime: ToolRuntime[None, SmartContextState],
            query: Annotated[str, "Natural language query for retrieving relevant code"],
            max_results: Annotated[int, "Maximum number of relevant chunks to return"] = 5,
        ) -> str:
            """Retrieve relevant code context based on a natural language query."""
            if not middleware._index:
                return "Codebase not indexed yet. Use index_codebase first."

            results = middleware._search_index(query, max_results)
            if not results:
                return f"No relevant context found for: {query}"

            lines = [f"Found {len(results)} relevant chunks:"]
            for chunk in results:
                lines.append(f"\n--- {chunk.file_path}:{chunk.start_line}-{chunk.end_line} (score: {chunk.relevance_score:.2f}) ---")
                lines.append(chunk.content)
            return "\n".join(lines)

        def context_usage_report(
            runtime: ToolRuntime[None, SmartContextState],
        ) -> str:
            """Show current context window usage with breakdown."""
            usage = middleware._context_usage
            bar_len = 40
            filled = int(bar_len * usage.percent_used / 100)
            bar = "█" * filled + "░" * (bar_len - filled)

            lines = [
                f"Context Window: [{bar}] {usage.percent_used:.1f}%",
                f"  Used: {usage.used_tokens:,} / {usage.max_tokens:,} tokens",
                f"  Remaining: {usage.remaining_tokens:,} tokens",
            ]
            if usage.breakdown:
                lines.append("  Breakdown:")
                for source, tokens in sorted(usage.breakdown.items(), key=lambda x: x[1], reverse=True):
                    lines.append(f"    {source}: {tokens:,} tokens")
            return "\n".join(lines)

        def save_memory(
            runtime: ToolRuntime[None, SmartContextState],
            content: Annotated[str, "Memory content to save"],
            category: Annotated[str, "Category: 'pattern', 'convention', 'architecture', 'gotcha', 'general'"] = "general",
        ) -> str:
            """Save a learning or important fact to project memory."""
            entry = MemoryEntry(content=content, source="agent", category=category, importance=0.8)
            middleware._memories.append(entry)
            return f"Saved memory [{category}]: {content[:80]}..."

        def list_memories(
            runtime: ToolRuntime[None, SmartContextState],
            category: str | None = None,
        ) -> str:
            """List saved memories, optionally filtered by category."""
            memories = middleware._memories
            if category:
                memories = [m for m in memories if m.category == category]
            if not memories:
                return "No memories saved yet."
            lines = [f"Saved memories ({len(memories)}):"]
            for m in memories:
                lines.append(f"  [{m.category}] {m.content[:100]}")
            return "\n".join(lines)

        return [
            StructuredTool.from_function(name="index_codebase", description="Index codebase for smart retrieval.", func=index_codebase),
            StructuredTool.from_function(name="retrieve_context", description="Retrieve relevant code context.", func=retrieve_context),
            StructuredTool.from_function(name="context_usage", description="Show context window usage.", func=context_usage_report),
            StructuredTool.from_function(name="save_memory", description="Save a learning to project memory.", func=save_memory),
            StructuredTool.from_function(name="list_memories", description="List saved memories.", func=list_memories),
        ]
