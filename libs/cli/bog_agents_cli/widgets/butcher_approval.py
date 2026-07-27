"""Plan-approval modal for the `/butcher` command (RD-5).

Butcher workers execute LLM-authored shell/edit commands in-place, bypassing the
CLI's normal per-tool HITL. Before any slice runs, the human sees the full slice
plan and approves it (unless `auto_approve` is set in butcher.toml). Slice titles
and file lists are model-authored, so they are escaped before display.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from bog_agents_cli.unicode_security import escape_for_display

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from bog_agents_cli.butcher import ButcherJob


class ButcherPlanApprovalScreen(ModalScreen[bool]):
    """Show the butcher slice plan and require approval. Dismisses True/False."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "confirm", "Run", show=False, priority=True),
        Binding("n,escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    ButcherPlanApprovalScreen {
        align: center middle;
    }

    ButcherPlanApprovalScreen > Vertical {
        width: 80;
        max-height: 80%;
        height: auto;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
    }

    ButcherPlanApprovalScreen .butcher-approve-title {
        text-style: bold;
        margin-bottom: 1;
    }

    ButcherPlanApprovalScreen .butcher-approve-slices {
        height: auto;
        max-height: 16;
        margin: 1 0;
    }

    ButcherPlanApprovalScreen .butcher-approve-help {
        text-align: center;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    """

    def __init__(self, job: ButcherJob) -> None:
        """Store the planned job to display.

        Args:
            job: The planned butcher job awaiting approval.
        """
        super().__init__()
        self._job = job

    def compose(self) -> ComposeResult:
        """Compose the plan-approval dialog.

        Yields:
            Widgets showing the slice plan and the approve/cancel prompt.
        """
        job = self._job
        with Vertical(id="butcher-approve"):
            yield Static(
                f"Run butcher job: {escape_for_display(job.title)}?",
                classes="butcher-approve-title",
            )
            yield Static(
                f"{len(job.slices)} slices. Workers execute model-authored "
                "shell/edit commands in-place, outside the normal approval gate."
            )
            with VerticalScroll(classes="butcher-approve-slices"):
                for s in job.slices:
                    files = ", ".join(s.files) if s.files else "(per instructions)"
                    check = (
                        f"  check: {s.acceptance_check}" if s.acceptance_check else ""
                    )
                    yield Static(
                        f"{s.number:02d}. {escape_for_display(s.title)}\n"
                        f"    files: {escape_for_display(files)}"
                        f"{escape_for_display(check)}"
                    )
            yield Static(
                "y / Enter to run, n / Esc to cancel",
                classes="butcher-approve-help",
            )

    def action_confirm(self) -> None:
        """Approve and run the plan."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Cancel; nothing is executed."""
        self.dismiss(False)
