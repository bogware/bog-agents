"""Tests for the wiring-pass tool bundles (background shell + memory search)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from bog_agents.backends.local_shell import LocalShellBackend
from bog_agents.tools import background_shell_tools_bundle, memory_search_tool_bundle

PY = sys.executable


class TestBackgroundShellToolsBundle:
    def test_returns_four_tools(self) -> None:
        be = LocalShellBackend(root_dir=tempfile.mkdtemp(), inherit_env=True)
        try:
            names = {t.name for t in background_shell_tools_bundle(be)}
            assert names == {
                "poll_background_command",
                "wait_background_command",
                "kill_background_command",
                "list_background_commands",
            }
        finally:
            be.close()

    def test_empty_for_backend_without_api(self) -> None:
        class _Plain:
            pass

        assert background_shell_tools_bundle(_Plain()) == []

    def test_tools_operate_on_a_real_background_command(self) -> None:
        be = LocalShellBackend(root_dir=tempfile.mkdtemp(), inherit_env=True)
        try:
            tools = {t.name: t for t in background_shell_tools_bundle(be)}
            resp = be.execute(f'{PY} -c "import time; time.sleep(20)"', background=True)
            tid = resp.output.split("task ", 1)[1].split(".", 1)[0].strip()
            # Call the underlying func directly (ToolRuntime is injected by the
            # agent at run time; the closures `del runtime` so None is fine).
            listed = tools["list_background_commands"].func(None)
            assert tid in listed
            killed = tools["kill_background_command"].func(None, tid)
            assert "Killed" in killed
        finally:
            be.close()


class TestMemorySearchToolBundle:
    def test_searches_memory_files(self, tmp_path: Path) -> None:
        a = tmp_path / "AGENTS.md"
        a.write_text("Use httpx not requests.\n\nConfig lives in ~/.bog-agents.", encoding="utf-8")
        b = tmp_path / "CLAUDE.md"
        b.write_text("Run the migration with --dry-run first.", encoding="utf-8")
        tools = memory_search_tool_bundle([a, b])
        assert [t.name for t in tools] == ["memory_search"]
        out = tools[0].func(None, "migration")
        assert "dry-run" in out
        assert "CLAUDE.md" in out  # names the source file

    def test_no_match_message(self, tmp_path: Path) -> None:
        a = tmp_path / "AGENTS.md"
        a.write_text("Use httpx not requests.", encoding="utf-8")
        out = memory_search_tool_bundle([a])[0].func(None, "kubernetes")
        assert "No memory matched" in out

    def test_empty_when_no_readable_sources(self, tmp_path: Path) -> None:
        assert memory_search_tool_bundle([tmp_path / "missing.md"]) == []

    def test_hybrid_path_with_injected_embedder(self, tmp_path: Path) -> None:
        # A toy embedder over a fixed vocab: the query embeds closest to the
        # chunk that shares its terms, so the vector fusion promotes it.
        vocab = ["auth", "token", "sandbox", "egress", "cost", "cache"]

        def embed(text: str) -> list[float]:
            words = text.lower().split()
            return [float(words.count(w)) for w in vocab]

        a = tmp_path / "AGENTS.md"
        a.write_text("auth token rotation\n\nsandbox egress proxy notes", encoding="utf-8")
        tools = memory_search_tool_bundle([a], embedder=embed)
        out = tools[0].func(None, "token")
        assert "auth token" in out

    def test_embedder_failure_degrades_to_keyword(self, tmp_path: Path) -> None:
        def broken(_text: str) -> list[float]:
            raise RuntimeError("embedder down")

        a = tmp_path / "AGENTS.md"
        a.write_text("run the migration with --dry-run", encoding="utf-8")
        # Index build must not crash when the embedder throws; keyword still works.
        tools = memory_search_tool_bundle([a], embedder=broken)
        assert "dry-run" in tools[0].func(None, "migration")


class TestEmbedderAdapter:
    def test_wraps_langchain_embeddings(self) -> None:
        from bog_agents.hybrid_memory import embedder_from_langchain

        class _FakeEmbeddings:
            def embed_query(self, text: str) -> list[float]:
                return [float(len(text)), 1.0]

        embed = embedder_from_langchain(_FakeEmbeddings())
        assert embed("abc") == [3.0, 1.0]


class TestMemoryEmbeddingLazyBatchPersist:
    """v6 SDK-5: embeddings are lazy, batched, persisted, and pruned with the sources."""

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        """Query-side embedder (the CLI passes both this and `embed_batch`)."""
        return [float(len(text)), float(text.count("alpha"))]

    @staticmethod
    def _batch(calls: list[list[str]]):  # noqa: ANN205 - test helper
        def embed_batch(texts: list[str]) -> list[list[float]]:
            calls.append(list(texts))
            return [[float(len(t)), float(t.count("alpha"))] for t in texts]

        return embed_batch

    def test_embed_batch_is_lazy_and_called_once(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        a = tmp_path / "AGENTS.md"
        a.write_text("alpha notes here\n\nbeta notes there", encoding="utf-8")
        tools = memory_search_tool_bundle([a], embedder=self._embed_one, embed_batch=self._batch(calls))
        assert calls == []  # nothing embedded at build time
        assert "alpha" in tools[0].func(None, "alpha")
        assert len(calls) == 1
        assert sorted(calls[0]) == ["alpha notes here", "beta notes there"]
        tools[0].func(None, "beta")
        assert len(calls) == 1  # vectors are stored, never recomputed

    def test_vectors_persist_across_rebuilds(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        db = tmp_path / "memory.db"
        a = tmp_path / "AGENTS.md"
        a.write_text("alpha notes here\n\nbeta notes there", encoding="utf-8")
        first = memory_search_tool_bundle([a], embedder=self._embed_one, embed_batch=self._batch(calls), db_path=db)
        first[0].func(None, "alpha")
        assert len(calls) == 1
        # A "restart": rebuild from the same sources against the same file.
        second = memory_search_tool_bundle([a], embedder=self._embed_one, embed_batch=self._batch(calls), db_path=db)
        assert "alpha" in second[0].func(None, "alpha")
        assert len(calls) == 1  # nothing re-embedded
        # New text is the only thing embedded after an edit.
        a.write_text("alpha notes here\n\nbeta notes there\n\ngamma is new", encoding="utf-8")
        third = memory_search_tool_bundle([a], embedder=self._embed_one, embed_batch=self._batch(calls), db_path=db)
        third[0].func(None, "gamma")
        assert len(calls) == 2
        assert calls[1] == ["gamma is new"]

    def test_stale_chunks_are_pruned_on_rebuild(self, tmp_path: Path) -> None:
        db = tmp_path / "memory.db"
        a = tmp_path / "AGENTS.md"
        a.write_text("obsolete guidance about kubernetes", encoding="utf-8")
        memory_search_tool_bundle([a], db_path=db)[0].func(None, "kubernetes")
        a.write_text("current guidance about docker", encoding="utf-8")
        tools = memory_search_tool_bundle([a], db_path=db)
        assert "No memory matched" in tools[0].func(None, "kubernetes")
        assert "docker" in tools[0].func(None, "docker")

    def test_embed_batch_failure_degrades_to_keyword(self, tmp_path: Path) -> None:
        attempts = 0

        def broken(_texts: list[str]) -> list[list[float]]:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("embedding service down")

        a = tmp_path / "AGENTS.md"
        a.write_text("run the migration with --dry-run", encoding="utf-8")
        tools = memory_search_tool_bundle([a], embedder=lambda _t: [1.0], embed_batch=broken)
        assert "dry-run" in tools[0].func(None, "migration")
        assert "dry-run" in tools[0].func(None, "migration")
        assert attempts == 1  # one attempt per process, not one per search


class TestHybridMemoryIndexPersistence:
    def test_add_without_vector_keeps_stored_embedding(self) -> None:
        from bog_agents.hybrid_memory import HybridMemoryIndex, MemoryChunk

        index = HybridMemoryIndex()
        index.add(MemoryChunk(text="alpha", chunk_id="c1", embedding=[1.0, 0.0]))
        index.add(MemoryChunk(text="alpha (edited)", chunk_id="c1"))
        assert index.chunks_missing_embeddings() == []
        assert index._load_chunk("c1").text == "alpha (edited)"
        assert list(index._load_chunk("c1").embedding) == [1.0, 0.0]

    def test_set_and_prune(self) -> None:
        from bog_agents.hybrid_memory import HybridMemoryIndex, MemoryChunk

        index = HybridMemoryIndex()
        index.add(MemoryChunk(text="alpha", chunk_id="c1"))
        index.add(MemoryChunk(text="beta", chunk_id="c2"))
        assert [cid for cid, _ in index.chunks_missing_embeddings()] == ["c1", "c2"]
        assert index.set_embeddings({"c1": [0.5, 0.5]}) == 1
        assert [cid for cid, _ in index.chunks_missing_embeddings()] == ["c2"]
        assert index.prune_except({"c1"}) == 1
        assert index._load_chunk("c2") is None
        assert index.search("beta") == []
