"""ROADMAP #69: the plan review model — parsing, comments, slice selection, revision and execution prompts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents_cli import plan_review as pr

PLAN = """# Ship the fix

1. Read the failing test
2. Patch parse_date
- [ ] Add a regression test
Some prose.
## Slice 1: Parser
files: a.py
## Slice 2: Tests
"""


def test_parse_and_review_model() -> None:
    review = pr.PlanReview.from_text(PLAN, title="Ship")
    kinds = [(line.number, line.kind) for line in review.lines]
    assert kinds[:5] == [
        (1, "heading"),
        (2, "text"),
        (3, "step"),
        (4, "step"),
        (5, "step"),
    ]
    assert (
        review.slice_ids == ["1", "2"]
        and review.lines[6].selectable
        and not review.lines[0].selectable
    )
    review.comment(4, "  use dateutil   instead ")
    review.comment(3, "x")
    review.comment(3, "")
    assert review.comments == {4: "use dateutil instead"}
    with pytest.raises(IndexError):
        review.comment(99, "nope")
    assert (
        review.toggle("2") is False
        and not review.selected("2")
        and review.toggle("2") is True
    )
    review.toggle("2")
    assert review.summary() == "Ship: 9 lines, 1 comment(s), 1/2 slices selected"

    prompt = review.revision_prompt(original_request="fix dates")
    assert (
        "line 4 (`2. Patch parse_date`): use dateutil instead" in prompt
        and "Original request:\nfix dates" in prompt
        and "deselected by the reviewer" in prompt
    )
    brief = review.execution_brief()
    assert (
        "## Slice 2: Tests  [SKIPPED by reviewer]" in brief
        and "## Slice 1: Parser" in brief
        and "line 4: use dateutil instead" in brief
    )
    assert pr.PlanReview.from_text("a\nb").revision_prompt() == ""
    assert pr.PlanReviewResult("approve", review).prompt.startswith(
        "Execute this approved plan"
    )
    assert (
        pr.PlanReviewResult("revise", review).prompt.startswith("Revise this plan plan")
        or "Revise this" in pr.PlanReviewResult("revise", review).prompt
    )
    assert pr.PlanReviewResult("cancel", review).prompt == ""


def test_sources_and_slice_selection(tmp_path: Path) -> None:
    job = tmp_path / ".bog-agents" / "butcher" / "job1"
    job.mkdir(parents=True)
    manifest = {
        "job_id": "job1",
        "title": "Split the work",
        "prompt": "do it",
        "slices": [
            {
                "number": 1,
                "title": "Parser",
                "files": ["a.py"],
                "acceptance_check": "pytest",
                "status": "pending",
            },
            {"number": 2, "title": "Tests", "files": [], "status": "done"},
        ],
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    review = pr.load_review("butcher", "job1", project_root=tmp_path)
    assert (
        review.kind == "butcher"
        and review.slice_ids == ["1", "2"]
        and "acceptance: pytest" in review.text
        and "status: done" in review.text
    )
    review.toggle("1")
    assert pr.apply_slice_selection(job / "manifest.json", review.deselected) == 1
    saved = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
    assert (
        saved["slices"][0]["status"] == "skipped"
        and saved["slices"][1]["status"] == "done"
    )
    assert (
        pr.apply_slice_selection(job / "manifest.json", set()) == 1
    )  # re-selected → pending again

    spec = tmp_path / ".bog-agents" / "jtbd" / "j1"
    spec.mkdir(parents=True)
    (spec / "job-spec.md").write_text("# Job\n- outcome one\n", encoding="utf-8")
    assert pr.load_review("jtbd", "j1", project_root=tmp_path).kind == "jtbd"
    (tmp_path / "plan.md").write_text("1. a\n", encoding="utf-8")
    assert pr.load_review("file", "plan.md", project_root=tmp_path).title == "plan.md"
    assert (
        pr.load_review("last", "", project_root=tmp_path, fallback_text="1. do\n")
        .lines[0]
        .kind
        == "step"
    )
    with pytest.raises(FileNotFoundError):
        pr.load_review("butcher", "missing", project_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        pr.load_review("last", "", project_root=tmp_path)
    with pytest.raises(ValueError, match="unknown plan source"):
        pr.load_review("bogus", "", project_root=tmp_path)
