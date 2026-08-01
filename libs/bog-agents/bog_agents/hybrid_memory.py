"""Hybrid local-RAG memory search (Tier-2 #8).

Grok Build indexes hand-editable Markdown memory with SQLite FTS5 (BM25) *and* a
vector KNN, then fuses the two (vector 0.7 / BM25 0.3), applies temporal decay to
session memory (curated memory is exempt), weights by source, and optionally
re-ranks with MMR for diversity. This module brings that ranking stack to bog.

Design for testability + light dependencies:
  * Keyword search uses SQLite FTS5 (BM25), with a `LIKE` fallback.
  * Vector search is **optional** and driven by an *injected* embedder
    (`Callable[[str], Sequence[float]]`) plus a pure-Python cosine — no numpy,
    no vector-DB service. Pass no embedder for keyword-only search.
  * Fusion, temporal decay, source weighting, and MMR are pure functions, so
    the ranking is unit-tested deterministically with a toy embedder.

The index is a cache over Markdown you can still hand-edit; it can be rebuilt at
any time. It backs the CLI `memory_search` tool (keyword by default; set
`BOG_AGENTS_MEMORY_VECTOR=1` to light up the vector path via
`embedder_from_langchain`).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Source kinds. Curated memory (global/workspace) is exempt from decay; session
# memory decays so stale chatter fades.
SOURCE_GLOBAL = "global"
SOURCE_WORKSPACE = "workspace"
SOURCE_SESSION = "session"

_DEFAULT_SOURCE_WEIGHTS = {SOURCE_GLOBAL: 1.0, SOURCE_WORKSPACE: 1.0, SOURCE_SESSION: 1.0}
_DECAYING_SOURCES = frozenset({SOURCE_SESSION})
_VECTOR_WEIGHT = 0.7
_BM25_WEIGHT = 0.3

Embedder = Callable[[str], Sequence[float]]


def embedder_from_langchain(embeddings: Any) -> Embedder:  # noqa: ANN401 - a LangChain Embeddings
    """Adapt a LangChain `Embeddings` object into an `Embedder` callable.

    Lets the hybrid vector path be lit by any provider's embeddings (Ollama,
    OpenAI, …) via `embeddings.embed_query`.

    Args:
        embeddings: A LangChain `Embeddings` instance (has `embed_query`).

    Returns:
        A `text -> vector` callable.
    """

    def _embed(text: str) -> Sequence[float]:
        return embeddings.embed_query(text)

    return _embed


@dataclass
class MemoryChunk:
    """One indexed unit of memory.

    Attributes:
        text: The chunk's Markdown text.
        source: One of `global` / `workspace` / `session`.
        timestamp: Unix time the chunk was written (for decay); 0 = unknown.
        chunk_id: Stable id (auto-generated when omitted).
        embedding: Optional dense vector for the vector-search path.
    """

    text: str
    source: str = SOURCE_WORKSPACE
    timestamp: float = 0.0
    chunk_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    embedding: Sequence[float] | None = None


@dataclass
class MemoryHit:
    """A ranked search result."""

    chunk: MemoryChunk
    score: float


# --- pure ranking helpers ---------------------------------------------------


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 for a zero vector)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _minmax(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a {id: score} map to [0, 1] (flat map → all 1.0)."""
    if not scores:
        return {}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi == lo:
        return dict.fromkeys(scores, 1.0)
    span = hi - lo
    return {k: (v - lo) / span for k, v in scores.items()}


def temporal_decay_factor(age_days: float, half_life_days: float) -> float:
    """Exponential decay factor in (0, 1]: 0.5 ** (age / half_life)."""
    if half_life_days <= 0 or age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def fuse_scores(
    bm25: dict[str, float],
    vector: dict[str, float],
    *,
    vector_weight: float = _VECTOR_WEIGHT,
    bm25_weight: float = _BM25_WEIGHT,
) -> dict[str, float]:
    """Fuse normalized BM25 + vector score maps into one {id: score}.

    Each map is min-max normalized first, then combined by weight; an id present
    in only one map contributes only that side.
    """
    nb = _minmax(bm25)
    nv = _minmax(vector)
    fused: dict[str, float] = {}
    for cid in set(nb) | set(nv):
        fused[cid] = bm25_weight * nb.get(cid, 0.0) + vector_weight * nv.get(cid, 0.0)
    return fused


def mmr_rerank(
    ranked_ids: list[str],
    relevance: dict[str, float],
    embeddings: dict[str, Sequence[float]],
    *,
    lambda_: float,
    k: int,
) -> list[str]:
    """Maximal Marginal Relevance re-rank to reduce redundancy.

    Args:
        ranked_ids: Candidate ids in relevance order (best first).
        relevance: {id: relevance score}.
        embeddings: {id: vector} (ids without a vector are appended in order).
        lambda_: Relevance/diversity trade-off in [0, 1] (1.0 = pure relevance).
        k: How many to return.

    Returns:
        Re-ranked ids (length <= k).
    """
    selected: list[str] = []
    remaining = [cid for cid in ranked_ids if cid in embeddings]
    no_vec = [cid for cid in ranked_ids if cid not in embeddings]
    while remaining and len(selected) < k:
        best_id = None
        best_score = -math.inf
        for cid in remaining:
            redundancy = max((cosine(embeddings[cid], embeddings[s]) for s in selected), default=0.0)
            score = lambda_ * relevance.get(cid, 0.0) - (1.0 - lambda_) * redundancy
            if score > best_score:
                best_score = score
                best_id = cid
        if best_id is None:
            break
        selected.append(best_id)
        remaining.remove(best_id)
    # Fill any remaining slots with vector-less candidates (relevance order).
    for cid in no_vec:
        if len(selected) >= k:
            break
        selected.append(cid)
    return selected[:k]


# --- the index --------------------------------------------------------------


class HybridMemoryIndex:
    """A hybrid (BM25 + optional vector) index over memory chunks.

    Backed by SQLite (FTS5 when available, else a LIKE keyword fallback). Vector
    search activates only when chunks carry embeddings and an embedder is passed
    to `search`.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """Open (creating if needed) the index at ``db_path`` (in-memory by default)."""
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._fts = self._detect_fts5()
        self._create_schema()

    def _detect_fts5(self) -> bool:
        try:
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _probe USING fts5(x)")
            self._conn.execute("DROP TABLE IF EXISTS _probe")
        except sqlite3.OperationalError:
            return False
        return True

    def _create_schema(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS chunks (chunk_id TEXT PRIMARY KEY, text TEXT, source TEXT, timestamp REAL, embedding TEXT)")
        if self._fts:
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text)")
        self._conn.commit()

    def add(self, chunk: MemoryChunk) -> str:
        """Index a single chunk (insert or replace by id). Returns the chunk id."""
        emb = json.dumps(list(chunk.embedding)) if chunk.embedding is not None else None
        self._conn.execute(
            "INSERT OR REPLACE INTO chunks (chunk_id, text, source, timestamp, embedding) VALUES (?, ?, ?, ?, ?)",
            (chunk.chunk_id, chunk.text, chunk.source, chunk.timestamp, emb),
        )
        if self._fts:
            self._conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk.chunk_id,))
            self._conn.execute("INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)", (chunk.chunk_id, chunk.text))
        self._conn.commit()
        return chunk.chunk_id

    def add_markdown(self, markdown: str, *, source: str = SOURCE_WORKSPACE, timestamp: float = 0.0) -> list[str]:
        """Chunk Markdown on blank lines and index each non-empty block."""
        return [
            self.add(MemoryChunk(text=block, source=source, timestamp=timestamp)) for block in (b.strip() for b in markdown.split("\n\n")) if block
        ]

    def _bm25(self, query: str) -> dict[str, float]:
        """Return {chunk_id: relevance} for the keyword match (higher = better)."""
        if self._fts:
            match = '"' + query.replace('"', '""') + '"'
            try:
                rows = self._conn.execute(
                    "SELECT chunk_id, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ?",
                    (match,),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
            # bm25() returns lower = better; flip so higher = better.
            return {cid: -score for cid, score in rows}
        like = f"%{query}%"
        rows = self._conn.execute("SELECT chunk_id FROM chunks WHERE text LIKE ?", (like,)).fetchall()
        return dict.fromkeys((r[0] for r in rows), 1.0)

    def _all_embeddings(self) -> dict[str, Sequence[float]]:
        rows = self._conn.execute("SELECT chunk_id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
        return {cid: json.loads(emb) for cid, emb in rows}

    def _load_chunk(self, chunk_id: str) -> MemoryChunk | None:
        row = self._conn.execute(
            "SELECT chunk_id, text, source, timestamp, embedding FROM chunks WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        emb = json.loads(row[4]) if row[4] else None
        return MemoryChunk(chunk_id=row[0], text=row[1], source=row[2], timestamp=row[3], embedding=emb)

    def search(
        self,
        query: str,
        *,
        embedder: Embedder | None = None,
        k: int = 8,
        source_weights: dict[str, float] | None = None,
        session_half_life_days: float = 7.0,
        mmr_lambda: float | None = None,
        now: float | None = None,
    ) -> list[MemoryHit]:
        """Hybrid search: fuse BM25 + vector, decay session memory, weight, MMR.

        Args:
            query: Free-text query.
            embedder: Optional query->vector function enabling the vector path.
            k: Max results.
            source_weights: Per-source multipliers (defaults to 1.0 each).
            session_half_life_days: Half-life for decaying `session` chunks.
            mmr_lambda: If set (0..1), MMR re-rank for diversity (needs embeddings).
            now: Reference time for decay (defaults to `time.time()`).

        Returns:
            Ranked `MemoryHit`s (empty on a blank query).
        """
        query = (query or "").strip()
        if not query:
            return []
        weights = {**_DEFAULT_SOURCE_WEIGHTS, **(source_weights or {})}
        reference = time.time() if now is None else now

        bm25 = self._bm25(query)
        vector: dict[str, float] = {}
        if embedder is not None:
            try:
                qvec = list(embedder(query))
                vector = {cid: cosine(qvec, emb) for cid, emb in self._all_embeddings().items()}
            except Exception:  # noqa: BLE001 - a failing embedder degrades to keyword-only, never crashes
                vector = {}

        fused = fuse_scores(bm25, vector)
        if not fused:
            return []

        # Apply per-source decay + source weighting.
        adjusted: dict[str, float] = {}
        for cid, base in fused.items():
            chunk = self._load_chunk(cid)
            if chunk is None:
                continue
            factor = weights.get(chunk.source, 1.0)
            if chunk.source in _DECAYING_SOURCES and chunk.timestamp:
                age_days = max(0.0, (reference - chunk.timestamp) / 86400.0)
                factor *= temporal_decay_factor(age_days, session_half_life_days)
            adjusted[cid] = base * factor

        ranked_ids = sorted(adjusted, key=lambda c: adjusted[c], reverse=True)
        if mmr_lambda is not None:
            embeddings = self._all_embeddings()
            ranked_ids = mmr_rerank(ranked_ids, adjusted, embeddings, lambda_=mmr_lambda, k=k)
        else:
            ranked_ids = ranked_ids[:k]

        hits: list[MemoryHit] = []
        for cid in ranked_ids:
            chunk = self._load_chunk(cid)
            if chunk is not None:
                hits.append(MemoryHit(chunk=chunk, score=adjusted.get(cid, 0.0)))
        return hits

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()


__all__ = [
    "SOURCE_GLOBAL",
    "SOURCE_SESSION",
    "SOURCE_WORKSPACE",
    "Embedder",
    "HybridMemoryIndex",
    "MemoryChunk",
    "MemoryHit",
    "cosine",
    "embedder_from_langchain",
    "fuse_scores",
    "mmr_rerank",
    "temporal_decay_factor",
]
