"""Tests for the read-only goal review modal (`/goal review`)."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from bog_agents_cli.goal_controller import GoalRecord
from bog_agents_cli.widgets.goal_review import GoalReviewScreen


class GoalReviewTestApp(App[None]):
    """Minimal app wrapper for testing GoalReviewScreen."""

    def compose(self) -> ComposeResult:
        yield Static("base")


def _body_text(screen: GoalReviewScreen) -> str:
    """Concatenate the rendered text of every body section."""
    from rich.markup import render as render_markup

    parts: list[str] = []
    for section in screen.query(".goal-review-section"):
        assert isinstance(section, Static)
        content = section._Static__content  # type: ignore[attr-defined]
        parts.append(render_markup(str(content)).plain)
    return "\n".join(parts)


class TestGoalReviewScreen:
    """The modal renders the goal objective, rubric, and status."""

    async def test_renders_objective_rubric_and_status(self) -> None:
        record = GoalRecord(
            objective="Ship the effort picker",
            rubric=["Lists valid levels", "Enter applies"],
            status="active",
            note="Halfway there",
        )
        app = GoalReviewTestApp()
        async with app.run_test() as pilot:
            screen = GoalReviewScreen(record)
            app.push_screen(screen)
            await pilot.pause()

            title = screen.query_one(".goal-review-title", Static)
            assert "Goal Review" in str(title._Static__content)  # type: ignore[attr-defined]

            body = _body_text(screen)
            assert "Ship the effort picker" in body
            assert "active" in body
            assert "Lists valid levels" in body
            assert "Enter applies" in body
            assert "Halfway there" in body

    async def test_no_goal_shows_empty_state(self) -> None:
        app = GoalReviewTestApp()
        async with app.run_test() as pilot:
            screen = GoalReviewScreen(GoalRecord())
            app.push_screen(screen)
            await pilot.pause()

            body = _body_text(screen)
            assert "No goal set" in body

    async def test_goal_without_rubric_notes_absence(self) -> None:
        record = GoalRecord(objective="Do the thing", status="blocked")
        app = GoalReviewTestApp()
        async with app.run_test() as pilot:
            screen = GoalReviewScreen(record)
            app.push_screen(screen)
            await pilot.pause()

            body = _body_text(screen)
            assert "Do the thing" in body
            assert "blocked" in body
            assert "none" in body.lower()

    async def test_markup_in_objective_renders_literally(self) -> None:
        """User-authored brackets must not crash Rich or be interpreted."""
        record = GoalRecord(
            objective="Support [bold] arrays and [nested] tags",
            rubric=["criterion with [red]markup[/red]"],
        )
        app = GoalReviewTestApp()
        async with app.run_test() as pilot:
            screen = GoalReviewScreen(record)
            # Mounting composes every escaped Static; a MarkupError would fail
            # the push_screen here.
            app.push_screen(screen)
            await pilot.pause()

            body = _body_text(screen)
            assert "[bold] arrays and [nested] tags" in body
            assert "[red]markup[/red]" in body

    async def test_escape_dismisses(self) -> None:
        app = GoalReviewTestApp()
        async with app.run_test() as pilot:
            dismissed: list[None] = []

            def on_dismiss(result: None) -> None:
                dismissed.append(result)

            screen = GoalReviewScreen(GoalRecord(objective="x"))
            app.push_screen(screen, on_dismiss)
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            assert dismissed == [None]
