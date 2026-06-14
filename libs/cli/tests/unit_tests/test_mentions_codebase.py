"""Tests for the @codebase semantic mention (ROADMAP #5)."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.mentions import (
    _resolve_codebase,
    get_mention_type_suggestions,
    parse_mentions,
    resolve_mentions,
)


class TestCodebaseMentionParsing:
    def test_parses_codebase_mention(self) -> None:
        tokens = parse_mentions("where is the @codebase:auth flow handled?")
        kinds = {(t.kind, t.value) for t in tokens}
        assert ("codebase", "auth") in kinds

    def test_codebase_in_suggestions(self) -> None:
        completions = {c for c, _ in get_mention_type_suggestions()}
        assert "@codebase:" in completions

    def test_search_still_present(self) -> None:
        # The semantic mention is additive, not a replacement for @search.
        completions = {c for c, _ in get_mention_type_suggestions()}
        assert "@search:" in completions


class TestResolveCodebase:
    def test_graceful_on_empty_dir(self, tmp_path: Path) -> None:
        # No files, no embedding model -> a clean string, never an exception.
        out = _resolve_codebase("nonexistent symbol xyz", tmp_path)
        assert isinstance(out, str)
        assert "codebase" in out.lower() or "no codebase matches" in out.lower()

    def test_resolve_mentions_injects_codebase_block(self, tmp_path: Path) -> None:
        (tmp_path / "thing.py").write_text("def banana():\n    return 1\n", encoding="utf-8")
        res = resolve_mentions("explain @codebase:banana", cwd=tmp_path)
        assert any(t.kind == "codebase" for t in res.tokens)
        # The augmented message prepends a context block for the mention.
        assert "@codebase:banana" in res.augmented
