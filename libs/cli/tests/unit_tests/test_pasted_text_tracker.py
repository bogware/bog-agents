"""Unit tests for `PastedTextTracker` and large-paste folding."""

from __future__ import annotations

from bog_agents_cli.input import (
    PASTED_TEXT_CHAR_THRESHOLD,
    PASTED_TEXT_LINE_THRESHOLD,
    PastedTextTracker,
)


class TestShouldFold:
    """Threshold rules for folding large pastes into a placeholder."""

    def test_short_single_line_not_folded(self) -> None:
        assert not PastedTextTracker.should_fold("hello world")

    def test_few_lines_not_folded(self) -> None:
        text = "\n".join("line" for _ in range(PASTED_TEXT_LINE_THRESHOLD - 1))
        assert not PastedTextTracker.should_fold(text)

    def test_many_lines_folded(self) -> None:
        text = "\n".join("line" for _ in range(PASTED_TEXT_LINE_THRESHOLD + 1))
        assert PastedTextTracker.should_fold(text)

    def test_long_single_line_folded(self) -> None:
        text = "x" * (PASTED_TEXT_CHAR_THRESHOLD + 1)
        assert PastedTextTracker.should_fold(text)


class TestAddAndExpand:
    """Round-trip storage + placeholder expansion."""

    def test_placeholder_format_includes_id_and_lines(self) -> None:
        tracker = PastedTextTracker()
        text = "a\nb\nc\nd\ne\nf"  # 6 lines
        placeholder = tracker.add(text)
        assert placeholder == "[Pasted #1: 6 lines]"

    def test_singular_for_one_line(self) -> None:
        tracker = PastedTextTracker()
        placeholder = tracker.add("just one line")
        assert placeholder == "[Pasted #1: 1 line]"

    def test_ids_increment(self) -> None:
        tracker = PastedTextTracker()
        first = tracker.add("a")
        second = tracker.add("b")
        assert first == "[Pasted #1: 1 line]"
        assert second == "[Pasted #2: 1 line]"

    def test_expand_restores_full_text(self) -> None:
        tracker = PastedTextTracker()
        original = "line1\nline2\nline3"
        placeholder = tracker.add(original)
        rendered = f"prefix {placeholder} suffix"
        assert tracker.expand(rendered) == f"prefix {original} suffix"

    def test_expand_unknown_id_kept_intact(self) -> None:
        tracker = PastedTextTracker()
        # Tracker is empty; placeholder doesn't match anything
        rendered = "[Pasted #99: 12 lines]"
        assert tracker.expand(rendered) == rendered

    def test_expand_empty_tracker_is_noop(self) -> None:
        tracker = PastedTextTracker()
        assert tracker.expand("hello") == "hello"


class TestSyncToText:
    """Pruning storage when the user deletes the placeholder."""

    def test_sync_drops_blocks_no_longer_referenced(self) -> None:
        tracker = PastedTextTracker()
        tracker.add("first")
        tracker.add("second")
        # User deleted both placeholders; text mentions neither
        tracker.sync_to_text("just plain typing")
        assert len(tracker) == 0

    def test_sync_keeps_referenced_block(self) -> None:
        tracker = PastedTextTracker()
        kept = tracker.add("first")
        tracker.add("second")
        tracker.sync_to_text(f"hello {kept} world")
        assert len(tracker) == 1
        # Surviving placeholder still expands
        assert tracker.expand(kept) == "first"

    def test_clear_resets_id_counter(self) -> None:
        tracker = PastedTextTracker()
        tracker.add("data")
        tracker.clear()
        next_placeholder = tracker.add("new")
        assert next_placeholder == "[Pasted #1: 1 line]"
