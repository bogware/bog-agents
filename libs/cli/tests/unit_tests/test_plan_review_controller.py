"""ROADMAP #69: `/review-plan` decisions and the review screen."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.widgets import Static

from bog_agents_cli import plan_review_controller as prc
from bog_agents_cli.plan_review import PlanReview, PlanReviewResult
from bog_agents_cli.widgets.plan_review_screen import PlanReviewScreen, line_label

if TYPE_CHECKING:
    import pytest

PLAN = "# Fix\n1. read\n## Slice 1: parser\n## Slice 2: tests\n"


def test_parse_target_and_build_review(tmp_path: Path) -> None:
    assert prc.parse_target("/review-plan") == ("last", "")
    assert prc.parse_target("/review-plan butcher job-1") == ("butcher", "job-1")
    assert prc.parse_target("/review-plan file docs/plan.md") == (
        "file",
        "docs/plan.md",
    )
    review = prc.build_review("/review-plan", project_root=tmp_path, last_text=PLAN)
    assert review.kind == "plan" and review.slice_ids == ["1", "2"]


def test_decide(tmp_path: Path) -> None:
    job = tmp_path / ".bog-agents" / "butcher" / "j"
    job.mkdir(parents=True)
    (job / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": "j",
                "title": "T",
                "slices": [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}],
            }
        ),
        encoding="utf-8",
    )
    review = prc.build_review(
        "/review-plan butcher j", project_root=tmp_path, last_text=""
    )
    review.toggle("2")
    prompt, note = prc.decide(PlanReviewResult("approve", review))
    assert (
        prompt is not None
        and "[SKIPPED by reviewer]" in prompt
        and "1 slice(s) marked" in note
    )
    assert (
        json.loads((job / "manifest.json").read_text(encoding="utf-8"))["slices"][1][
            "status"
        ]
        == "skipped"
    )
    assert prc.decide(None) == (None, "Plan review cancelled; nothing sent.")
    assert prc.decide(PlanReviewResult("revise", review))[0] is None
    review.comment(1, "rename it")
    prompt, note = prc.decide(PlanReviewResult("revise", review))
    assert prompt is not None and "rename it" in prompt and "1 comment(s)" in note


class _Host(App[None]):
    def __init__(self, review: PlanReview) -> None:
        super().__init__()
        self.review = review
        self.results: list[PlanReviewResult | None] = []

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        self.push_screen(PlanReviewScreen(self.review), self.results.append)


async def test_screen_comment_toggle_and_approve() -> None:
    review = PlanReview.from_text(PLAN, title="Fix")
    host = _Host(review)
    async with host.run_test() as pilot:
        await pilot.pause()
        screen = host.screen
        assert isinstance(screen, PlanReviewScreen)
        await pilot.press("down", "down")  # line 3: Slice 1
        await pilot.press("space")
        assert not review.selected("1") and line_label(review, 3).startswith("[ ]")
        await pilot.press("c")
        await pilot.press(*"too big")
        await pilot.press("enter")
        assert review.comments == {3: "too big"} and "💬" in line_label(review, 3)
        await pilot.press("a")
        await pilot.pause()
    assert (
        host.results
        and host.results[0] is not None
        and host.results[0].action == "approve"
    )
    assert "[SKIPPED by reviewer]" in host.results[0].prompt


async def test_screen_revise_needs_a_comment_and_escape_cancels() -> None:
    review = PlanReview.from_text(PLAN, title="Fix")
    host = _Host(review)
    async with host.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert not host.results  # refused: no comments yet
        await pilot.press("escape")
        await pilot.pause()
    assert host.results == [None]


def test_run_review_plan_command_reports_missing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from bog_agents_cli import findings_controller
    from bog_agents_cli.widgets import messages

    class _App:
        def __init__(self) -> None:
            self.messages: list[object] = []
            self._cwd = str(tmp_path)

        async def _mount_message(self, message: object) -> None:
            self.messages.append(message)

        def _get_last_assistant_text(self) -> str:
            return ""

    monkeypatch.setattr(findings_controller, "project_root", lambda _app: tmp_path)
    monkeypatch.setattr(messages, "ErrorMessage", lambda text: ("error", text))
    app = _App()
    asyncio.run(prc.run_review_plan_command(app, "/review-plan butcher nope"))
    assert (
        app.messages == [("error", app.messages[0][1])]
        and "no butcher job" in app.messages[0][1]
    )  # type: ignore[index]
