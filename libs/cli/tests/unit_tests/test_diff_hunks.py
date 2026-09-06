"""ROADMAP #66: hunk parsing and single-hunk revert."""

from __future__ import annotations

from bog_agents_cli.diff_hunks import (
    new_side,
    old_side,
    parse_hunks,
    render_hunk,
    revert_hunk,
)
from bog_agents_cli.file_ops import compute_unified_diff

BEFORE = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
AFTER = "a\nB\nc\nd\ne\nf\ng\nh\nI\nj\nk\n"


def _diff() -> str:
    return (
        compute_unified_diff(BEFORE, AFTER, "x.txt", max_lines=None, context_lines=1)
        or ""
    )


def test_parse_two_hunks() -> None:
    hunks = parse_hunks(_diff())
    assert len(hunks) == 2
    assert (hunks[0].old_start, hunks[0].new_start) == (1, 1)
    assert hunks[0].removed == 1 and hunks[0].added == 1
    assert old_side(hunks[0]) == ["a", "b", "c"]
    assert new_side(hunks[0]) == ["a", "B", "c"]
    assert render_hunk(hunks[0]).startswith("@@ -1,3 +1,3 @@")


def test_revert_each_hunk_independently() -> None:
    hunks = parse_hunks(_diff())
    only_second = revert_hunk(AFTER, hunks[0])
    assert only_second == "a\nb\nc\nd\ne\nf\ng\nh\nI\nj\nk\n"
    only_first = revert_hunk(AFTER, hunks[1])
    assert only_first == "a\nB\nc\nd\ne\nf\ng\nh\ni\nj\n"
    both = revert_hunk(only_second, hunks[1])
    assert both == BEFORE


def test_revert_missing_hunk_returns_none() -> None:
    hunks = parse_hunks(_diff())
    assert revert_hunk("totally different\n", hunks[0]) is None
    assert parse_hunks("no hunks here") == []
