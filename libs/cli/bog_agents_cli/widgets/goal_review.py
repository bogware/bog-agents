"""Read-only goal review modal for `/goal review`.

A small scrollable `ModalScreen` that shows the current goal at a glance: the
objective, its lifecycle status, the acceptance-criteria rubric, and the latest
progress/blocker note. It mutates nothing — the caller loads a
:class:`~bog_agents_cli.goal_controller.GoalRecord` (already merged with live
agent state) and hands it in; Esc closes the view.

Goal text is user-authored free-form (it may contain `[`/`]` that Rich would
otherwise treat as markup), so every interpolated field is markup-escaped before
rendering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from bog_agents_cli.goal_controller import GoalRecord


class GoalReviewScreen(ModalScreen[None]):
    """Read-only modal showing the current goal objective, rubric, and status."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("q", "cancel", "Close", show=False, priority=True),
        Binding("up", "scroll_up", "Up", show=False, priority=True),
        Binding("down", "scroll_down", "Down", show=False, priority=True),
        Binding("pageup", "page_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page down", show=False, priority=True),
    ]

    CSS = """
    GoalReviewScreen {
        align: center middle;
    }

    GoalReviewScreen > Vertical {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }

    GoalReviewScreen .goal-review-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    GoalReviewScreen .goal-review-body {
        height: auto;
        max-height: 22;
    }

    GoalReviewScreen .goal-review-section {
        margin-bottom: 1;
    }

    GoalReviewScreen .goal-review-help {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, record: GoalRecord) -> None:
        """Initialize the goal review screen.

        Args:
            record: The goal to display (read-only; never mutated).
        """
        super().__init__()
        self._record = record

    def compose(self) -> ComposeResult:
        """Compose the review layout.

        Yields:
            Widgets for the read-only goal review UI.
        """
        from rich.markup import escape as escape_markup

        with Vertical():
            yield Static("Goal Review", classes="goal-review-title")
            with VerticalScroll(classes="goal-review-body"):
                record = self._record
                if not record.is_set:
                    yield Static(
                        "No goal set.\n\n"
                        "Set one with [bold]/goal <objective>[/bold], then draft "
                        "acceptance criteria with [bold]/rubric draft[/bold].",
                        classes="goal-review-section",
                    )
                else:
                    yield Static(
                        f"[bold]Objective[/bold]\n{escape_markup(record.objective)}",
                        classes="goal-review-section",
                    )
                    yield Static(
                        f"[bold]Status[/bold]  [dim]{escape_markup(record.status)}[/dim]",
                        classes="goal-review-section",
                    )
                    if record.rubric:
                        lines = ["[bold]Acceptance criteria[/bold]"]
                        lines.extend(
                            f"  {i}. {escape_markup(c)}"
                            for i, c in enumerate(record.rubric, start=1)
                        )
                        yield Static("\n".join(lines), classes="goal-review-section")
                    else:
                        yield Static(
                            "[bold]Acceptance criteria[/bold]\n"
                            "  [dim](none — draft with /rubric draft)[/dim]",
                            classes="goal-review-section",
                        )
                    if record.note:
                        yield Static(
                            f"[bold]Latest note[/bold]\n{escape_markup(record.note)}",
                            classes="goal-review-section",
                        )
            yield Static("Esc close", classes="goal-review-help")

    def _body(self) -> VerticalScroll:
        """Return the scrollable body container."""
        return self.query_one(".goal-review-body", VerticalScroll)

    def action_scroll_up(self) -> None:
        """Scroll the body up one line."""
        self._body().scroll_up()

    def action_scroll_down(self) -> None:
        """Scroll the body down one line."""
        self._body().scroll_down()

    def action_page_up(self) -> None:
        """Scroll the body up one page."""
        self._body().scroll_page_up()

    def action_page_down(self) -> None:
        """Scroll the body down one page."""
        self._body().scroll_page_down()

    def action_cancel(self) -> None:
        """Close the review."""
        self.dismiss(None)
