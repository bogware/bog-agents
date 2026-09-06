"""ROADMAP #75: memory rebuild — parse, dedup, model consolidation with fallback, candidate / apply / discard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents_cli import memory_rebuild as mr

STORE = """# Project notes

Some hand-written intro.

## Agent-Recorded Memories
<!-- bog-agents auto-memories: written by the agent via the `remember` tool. Safe to edit, reorganize, or delete. -->

- (convention) Run tests with make test
- (gotcha) The CLI wraps lines at 88 columns
- (convention) Run   tests with make test.
- (decision) Use uv for everything

## Other section
Keep me.
"""


def test_parse_compose_round_trip_and_dedup() -> None:
    before, entries, after = mr.parse_entries(STORE)
    assert before.startswith("# Project notes") and after.startswith(
        "\n## Other section"
    )
    assert [(e.category, e.text) for e in entries][:2] == [
        ("convention", "Run tests with make test"),
        ("gotcha", "The CLI wraps lines at 88 columns"),
    ]
    kept, notes = mr.dedup_entries(entries)
    assert len(kept) == 3 and notes == [
        "dropped duplicate: - (convention) Run   tests with make test."
    ]
    text = mr.compose(before, kept, after)
    assert (
        text.count("Run tests with make test") == 1
        and "Keep me." in text
        and "Some hand-written intro." in text
    )
    again = mr.parse_entries(text)[1]
    assert again == kept
    assert mr.parse_entries("no section here") == ("no section here", [], "")


def test_consolidate_with_model_and_fallbacks() -> None:
    entries = mr.parse_entries(STORE)[1]
    transcripts = [("t1", "We switched from make test to `uv run pytest` last week.")]
    prompts: list[str] = []

    def _invoke(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "entries": [
                    {
                        "text": "Run tests with uv run pytest",
                        "category": "convention",
                        "sources": ["entry:1", "thread:t1"],
                    },
                    {
                        "text": "The CLI wraps lines at 88 columns",
                        "category": "gotcha",
                        "sources": ["entry:2"],
                    },
                    {"text": "The CLI wraps lines at 88 columns", "category": "gotcha"},
                    {"text": "", "category": "note"},
                    "junk",
                ],
                "notes": ["replaced make test (contradicted by thread t1)"],
            }
        )

    rebuilt, report = mr.consolidate(
        entries, transcripts, invoke=_invoke, steer="prefer uv"
    )
    assert report.mode == "model" and [e.text for e in rebuilt] == [
        "Run tests with uv run pytest",
        "The CLI wraps lines at 88 columns",
    ]
    assert (
        rebuilt[0].sources == ("entry:1", "thread:t1")
        and "contradicted" in report.notes[0]
        and any("duplicate" in n for n in report.notes)
    )
    assert (
        "Operator steering: prefer uv" in prompts[0]
        and "### thread:t1" in prompts[0]
        and "1. (convention) Run tests with make test" in prompts[0]
    )

    rebuilt, report = mr.consolidate(entries, transcripts, invoke=lambda _p: "not json")
    assert (
        report.mode == "dedup"
        and len(rebuilt) == 3
        and any("unusable" in n for n in report.notes)
    )
    rebuilt, report = mr.consolidate(entries, [], invoke=None)
    assert (
        report.mode == "dedup"
        and report.after_count == 3
        and "3 → 3" not in report.summary()
        and "4 → 3" in report.summary()
    )


def test_rebuild_apply_and_discard(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text(STORE, encoding="utf-8")
    candidate = mr.rebuild(target, project_root=tmp_path, invoke=None)
    assert (
        candidate.changed
        and candidate.path.is_file()
        and candidate.diff_path.read_text(encoding="utf-8").startswith(
            "--- a/AGENTS.md"
        )
    )
    assert json.loads(candidate.report_path.read_text(encoding="utf-8"))[
        "target"
    ] == str(target)
    assert mr.pending_candidate(tmp_path) == (candidate.path, target)
    backup = mr.apply_candidate(tmp_path)
    assert backup.read_text(encoding="utf-8") == STORE
    assert (
        target.read_text(encoding="utf-8").count("make test") == 1
        and mr.pending_candidate(tmp_path) is None
    )
    with pytest.raises(FileNotFoundError):
        mr.apply_candidate(tmp_path)

    unchanged = mr.rebuild(target, project_root=tmp_path, invoke=None)
    assert not unchanged.changed
    assert mr.discard_candidate(tmp_path) and not mr.discard_candidate(tmp_path)

    fresh = mr.rebuild(
        tmp_path / "missing.md",
        project_root=tmp_path,
        invoke=lambda _p: json.dumps(
            {"entries": [{"text": "New fact", "category": "note"}]}
        ),
    )
    assert fresh.changed and "New fact" in fresh.path.read_text(encoding="utf-8")
