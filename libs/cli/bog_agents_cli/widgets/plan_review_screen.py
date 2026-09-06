"""`PlanReviewScreen` (ROADMAP #69): review a plan line by line before it runs.

One modal for butcher manifests, JTBD specs, plan-mode output and any plan
file: the lines are listed with their numbers, `c` stages a comment on the
highlighted line, `space` toggles a slice checkbox, `a` approves (the caller
sends `execution_brief()`), `r` asks for a revision (the caller sends
`revision_prompt()`), `escape` cancels. The screen owns no model calls — it
returns a `PlanReviewResult` and the controller decides what to send.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from bog_agents_cli.plan_review import PlanReview, PlanReviewResult

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType


def line_label(review: PlanReview, number: int) -> str:
    """The rendered row for line `number`: checkbox, number, text, comment marker."""
    line = review.lines[number - 1]
    box = ""
    if line.selectable and line.slice_id is not None:
        box = "[x] " if review.selected(line.slice_id) else "[ ] "
    marker = "  💬 " + review.comments[number] if number in review.comments else ""
    return f"{box}{number:>3}  {line.text}{marker}"


class PlanReviewScreen(ModalScreen["PlanReviewResult | None"]):
    """Line-addressed review of a plan; returns the reviewer's decision."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("c", "comment", "Comment on line"),
        Binding("space", "toggle", "Toggle slice"),
        Binding("a", "approve", "Approve"),
        Binding("r", "revise", "Request revision"),
    ]

    DEFAULT_CSS = """
    PlanReviewScreen {
        align: center middle;
    }
    PlanReviewScreen > Vertical {
        width: 110;
        max-width: 95%;
        height: 90%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }
    PlanReviewScreen #plan-title {
        text-style: bold;
        color: $primary;
    }
    PlanReviewScreen #plan-summary {
        color: $text-muted;
        margin-bottom: 1;
    }
    PlanReviewScreen ListView {
        height: 1fr;
    }
    PlanReviewScreen #plan-comment {
        margin-top: 1;
    }
    PlanReviewScreen #plan-help {
        color: $text-muted;
        height: 1;
    }
    """

    def __init__(self, review: PlanReview) -> None:
        super().__init__()
        self.review = review
        self._commenting: int | None = None

    def compose(self) -> ComposeResult:
        """Build the review layout.

        Yields:
            The title, summary, line list, comment box and key help.
        """
        with Vertical():
            yield Label(self.review.title, id="plan-title")
            yield Static(self.review.summary(), id="plan-summary")
            yield ListView(
                *[
                    ListItem(
                        Static(line_label(self.review, line.number)),
                        id=f"plan-line-{line.number}",
                    )
                    for line in self.review.lines
                ],
                id="plan-lines",
            )
            yield Input(
                placeholder="Comment on the highlighted line, Enter to stage, Escape to cancel",
                id="plan-comment",
            )
            with Horizontal():
                yield Static(
                    "c comment · space toggle slice · a approve · r revise · esc cancel",
                    id="plan-help",
                )

    def on_mount(self) -> None:
        """Hide the comment box until `c`."""
        self.query_one("#plan-comment", Input).display = False
        self.query_one("#plan-lines", ListView).focus()

    def _current_number(self) -> int | None:
        lines = self.query_one("#plan-lines", ListView)
        index = lines.index
        return None if index is None else index + 1

    def _refresh_line(self, number: int) -> None:
        item = self.query_one(f"#plan-line-{number}", ListItem)
        item.query_one(Static).update(line_label(self.review, number))
        self.query_one("#plan-summary", Static).update(self.review.summary())

    def action_comment(self) -> None:
        """Open the comment box for the highlighted line."""
        number = self._current_number()
        if number is None:
            return
        self._commenting = number
        box = self.query_one("#plan-comment", Input)
        box.value = self.review.comments.get(number, "")
        box.display = True
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Stage the comment and return to the list."""
        if event.input.id != "plan-comment" or self._commenting is None:
            return
        number = self._commenting
        self.review.comment(number, event.value)
        self._refresh_line(number)
        self._commenting = None
        event.input.display = False
        event.input.value = ""
        self.query_one("#plan-lines", ListView).focus()

    def action_toggle(self) -> None:
        """Flip the slice checkbox on the highlighted line."""
        number = self._current_number()
        if number is None:
            return
        line = self.review.lines[number - 1]
        if line.slice_id is None:
            return
        self.review.toggle(line.slice_id)
        self._refresh_line(number)

    def action_approve(self) -> None:
        """Approve the plan as reviewed."""
        self.dismiss(PlanReviewResult("approve", self.review))

    def action_revise(self) -> None:
        """Ask the planner for a revision (needs at least one comment)."""
        if not self.review.comments:
            self.notify(
                "Stage at least one comment (c) before requesting a revision.",
                severity="warning",
            )
            return
        self.dismiss(PlanReviewResult("revise", self.review))

    def action_cancel(self) -> None:
        """Close without acting; an open comment box just closes."""
        box = self.query_one("#plan-comment", Input)
        if box.display:
            box.display = False
            self._commenting = None
            self.query_one("#plan-lines", ListView).focus()
            return
        self.dismiss(None)


__all__ = ["PlanReviewScreen", "line_label"]
