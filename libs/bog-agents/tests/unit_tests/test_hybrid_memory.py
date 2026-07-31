"""Tests for the hybrid local-RAG memory ranking stack (Tier-2 #8)."""

from __future__ import annotations

from bog_agents.hybrid_memory import (
    SOURCE_GLOBAL,
    SOURCE_SESSION,
    HybridMemoryIndex,
    MemoryChunk,
    cosine,
    fuse_scores,
    mmr_rerank,
    temporal_decay_factor,
)


def _bow(text: str) -> list[float]:
    """A toy deterministic embedder: bag-of-words over a tiny fixed vocab."""
    vocab = ["auth", "token", "session", "cost", "cache", "sandbox", "test", "lint"]
    words = text.lower().split()
    return [float(words.count(w)) for w in vocab]


class TestPureHelpers:
    def test_cosine_identical_is_one(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0

    def test_cosine_orthogonal_is_zero(self) -> None:
        assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_cosine_zero_vector_safe(self) -> None:
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_temporal_decay_halves_at_half_life(self) -> None:
        assert temporal_decay_factor(7.0, 7.0) == 0.5
        assert temporal_decay_factor(0.0, 7.0) == 1.0
        assert temporal_decay_factor(14.0, 7.0) == 0.25

    def test_fuse_weights_vector_higher(self) -> None:
        fused = fuse_scores({"a": 1.0, "b": 0.0}, {"a": 0.0, "b": 1.0})
        # vector (weight 0.7) beats bm25 (0.3), so b outranks a.
        assert fused["b"] > fused["a"]

    def test_mmr_penalizes_redundancy(self) -> None:
        # a and b are identical vectors; c is orthogonal. With diversity on, the
        # second pick should be c, not the near-duplicate b.
        emb = {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [0.0, 1.0]}
        rel = {"a": 1.0, "b": 0.9, "c": 0.8}
        out = mmr_rerank(["a", "b", "c"], rel, emb, lambda_=0.5, k=2)
        assert out[0] == "a"
        assert out[1] == "c"


class TestHybridIndex:
    def test_keyword_only_search(self) -> None:
        idx = HybridMemoryIndex()
        try:
            idx.add(MemoryChunk(text="we removed the global auth session", source=SOURCE_GLOBAL))
            idx.add(MemoryChunk(text="the cost ledger caps spend", source=SOURCE_GLOBAL))
            hits = idx.search("auth")
            assert hits and "auth" in hits[0].chunk.text
        finally:
            idx.close()

    def test_hybrid_uses_vector_when_embedder_given(self) -> None:
        idx = HybridMemoryIndex()
        try:
            idx.add(MemoryChunk(text="auth token rotation", source=SOURCE_GLOBAL, embedding=_bow("auth token rotation")))
            idx.add(MemoryChunk(text="sandbox egress proxy", source=SOURCE_GLOBAL, embedding=_bow("sandbox egress proxy")))
            # Query semantically closest to the auth chunk.
            hits = idx.search("token", embedder=_bow, k=2)
            assert hits[0].chunk.text.startswith("auth token")
        finally:
            idx.close()

    def test_session_decay_demotes_old_session_memory(self) -> None:
        idx = HybridMemoryIndex()
        try:
            now = 1_000_000_000.0
            # Two equally-matching session chunks; one is fresh, one is 30 days old.
            idx.add(MemoryChunk(text="session note about cache", source=SOURCE_SESSION, timestamp=now))
            old = MemoryChunk(text="session note about cache", source=SOURCE_SESSION, timestamp=now - 30 * 86400)
            idx.add(old)
            hits = idx.search("cache", k=2, now=now, session_half_life_days=7.0)
            # The fresh chunk must outrank the decayed one.
            assert hits[0].chunk.timestamp == now
            assert hits[0].score > hits[1].score
        finally:
            idx.close()

    def test_curated_memory_exempt_from_decay(self) -> None:
        idx = HybridMemoryIndex()
        try:
            now = 1_000_000_000.0
            # A global chunk with an ancient timestamp must NOT be decayed.
            idx.add(MemoryChunk(text="global cache policy", source=SOURCE_GLOBAL, timestamp=now - 365 * 86400))
            hits = idx.search("cache", now=now)
            assert hits and hits[0].score > 0.0
        finally:
            idx.close()

    def test_source_weight_applied(self) -> None:
        idx = HybridMemoryIndex()
        try:
            idx.add(MemoryChunk(text="cache tuning tip", source=SOURCE_GLOBAL, chunk_id="g"))
            idx.add(MemoryChunk(text="cache tuning tip", source=SOURCE_SESSION, chunk_id="s"))
            hits = idx.search("cache", source_weights={SOURCE_GLOBAL: 2.0, SOURCE_SESSION: 0.5})
            top = hits[0]
            assert top.chunk.source == SOURCE_GLOBAL
        finally:
            idx.close()

    def test_add_markdown_chunks_on_blank_lines(self) -> None:
        idx = HybridMemoryIndex()
        try:
            ids = idx.add_markdown("first block about auth\n\nsecond block about cost", source=SOURCE_GLOBAL)
            assert len(ids) == 2
            assert {h.chunk.text for h in idx.search("cost")} == {"second block about cost"}
        finally:
            idx.close()

    def test_blank_query_empty(self) -> None:
        idx = HybridMemoryIndex()
        assert idx.search("   ") == []
        idx.close()
