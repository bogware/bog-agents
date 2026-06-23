"""Hardening tests for hybrid_search EmbeddingCache persistence (S22).

Verifies that ``EmbeddingCache.save`` degrades to in-memory-only on an
unwritable cache location (read-only checkout, full disk, Windows path-length
limits) instead of raising an unhandled ``OSError`` into the tool caller and
discarding all embedding compute. ``RepoMapCache.save`` already wraps the
identical mkdir+write_text pattern in ``except OSError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.middleware.hybrid_search import EmbeddingCache

if TYPE_CHECKING:
    from pathlib import Path


def test_save_swallows_oserror_when_parent_is_a_file(tmp_path: Path) -> None:
    """A cache root that collides with an existing file must not crash save().

    Creating the cache parent directory fails with ``OSError`` (a file already
    occupies the path), which previously propagated out of ``save`` and aborted
    the embedding tool call. The error must be swallowed.
    """
    # Occupy the directory that EmbeddingCache will try to mkdir under.
    blocker = tmp_path / "root"
    blocker.write_text("not a directory", encoding="utf-8")

    cache = EmbeddingCache(blocker)
    cache._entries = {"foo.py": {"hash": "abc", "vectors": []}}

    # Must not raise even though mkdir on a path under a file fails.
    cache.save()

    # Degraded to in-memory-only: nothing was persisted.
    assert not cache._cache_path.exists()


def test_save_swallows_oserror_from_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``OSError`` raised while writing the payload is swallowed."""
    cache = EmbeddingCache(tmp_path)
    cache._entries = {"bar.py": {"hash": "def", "vectors": []}}

    def _boom(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("disk full")

    # Patch Path.write_text so the write step fails after mkdir succeeds.
    monkeypatch.setattr("pathlib.Path.write_text", _boom)

    # Must not raise — the embedding compute is preserved in memory.
    cache.save()


def test_save_persists_when_writable(tmp_path: Path) -> None:
    """The happy path still writes the cache so the fix did not break save()."""
    cache = EmbeddingCache(tmp_path)
    cache._entries = {"baz.py": {"hash": "ghi", "vectors": []}}

    cache.save()

    assert cache._cache_path.exists()
    assert "baz.py" in cache._cache_path.read_text(encoding="utf-8")
