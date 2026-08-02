"""Tests for the session full-text search index (Tier-1 #4)."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.session_search import SessionSearchIndex


def _index(tmp_path: Path) -> SessionSearchIndex:
    return SessionSearchIndex(tmp_path / "sessions_fts.db")


class TestSessionSearchIndex:
    def test_index_and_find_by_body(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index(
                "t1",
                "Refactor auth",
                "We removed the global session and added a token store",
            )
            idx.index(
                "t2", "Fix CI", "The lockfile drifted so the lock-check job failed"
            )
            hits = idx.search("lockfile")
            assert [h.thread_id for h in hits] == ["t2"]

    def test_find_by_title(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "Refactor auth", "body one")
            idx.index("t2", "Fix CI", "body two")
            hits = idx.search("auth")
            assert "t1" in {h.thread_id for h in hits}

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "Refactor auth", "token store")
            assert idx.search("kubernetes") == []

    def test_blank_query_returns_empty(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "title", "body")
            assert idx.search("   ") == []

    def test_reindex_replaces_not_duplicates(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "v1", "alpha content")
            idx.index("t1", "v2", "beta content")
            assert idx.count() == 1
            assert idx.search("alpha") == []  # old text gone
            assert {h.thread_id for h in idx.search("beta")} == {"t1"}

    def test_remove_and_clear(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "a", "one")
            idx.index("t2", "b", "two")
            idx.remove("t1")
            assert idx.count() == 1
            idx.clear()
            assert idx.count() == 0

    def test_query_with_punctuation_does_not_crash(self, tmp_path: Path) -> None:
        # FTS5 operators / quotes in user text must be treated as literal terms.
        with _index(tmp_path) as idx:
            idx.index("t1", "title", "handle the OR/AND edge-case in the parser")
            # Should not raise an FTS syntax error.
            hits = idx.search('OR AND "edge')
            assert isinstance(hits, list)

    def test_persists_across_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        idx = SessionSearchIndex(db)
        idx.index("t1", "persisted", "durable content here")
        idx.close()
        reopened = SessionSearchIndex(db)
        try:
            assert {h.thread_id for h in reopened.search("durable")} == {"t1"}
        finally:
            reopened.close()

    def test_snippet_present_for_body_match(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "title", "the quick brown fox jumps over the lazy dog")
            hits = idx.search("brown")
            assert hits and hits[0].snippet  # non-empty excerpt


class TestSnippetRendersSafely:
    """FTS snippets are Rich-hostile: every match is wrapped in `[`...`]`."""

    def test_snippet_wraps_matches_in_brackets(self, tmp_path: Path) -> None:
        with _index(tmp_path) as idx:
            idx.index("t1", "Ship it", "we should deploy the service tomorrow")
            hits = idx.search("deploy")
            assert hits, "expected a hit"
            assert "[deploy]" in hits[0].snippet

    def test_formatted_results_survive_markup_rendering(self, tmp_path: Path) -> None:
        # Unescaped, Rich either eats a match naming a real style or drops the
        # whole message to literal on one that does not.
        from bog_agents_cli.session_search import format_search_results
        from bog_agents_cli.widgets.messages import _safe_render_markup

        with _index(tmp_path) as idx:
            idx.index("t1", "Ship it", "we should deploy the service")
            idx.index("t2", "Styling", "make the text bold please")
            for term in ("deploy", "bold"):
                hits = idx.search(term)
                assert hits, term
                rendered = str(_safe_render_markup(format_search_results(term, hits)))
                # Styling consumed, highlight markers preserved verbatim.
                assert "[bold]Threads" not in rendered, term
                assert f"[{term}]" in rendered, term

    def test_hostile_title_does_not_break_rendering(self, tmp_path: Path) -> None:
        from bog_agents_cli.session_search import format_search_results
        from bog_agents_cli.widgets.messages import _safe_render_markup

        with _index(tmp_path) as idx:
            idx.index("t1", "[red]not a tag[/red]", "deploy the service")
            hits = idx.search("deploy")
            rendered = str(_safe_render_markup(format_search_results("deploy", hits)))
            assert "[red]not a tag[/red]" in rendered

    def test_no_hits_message(self) -> None:
        from bog_agents_cli.session_search import format_search_results

        assert format_search_results("nope", []) == "No threads matched 'nope'."
