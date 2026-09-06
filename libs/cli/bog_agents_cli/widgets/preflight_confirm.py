"""Cost pre-flight confirmation modal (ROADMAP #51).

Shown before `/team run`, `/butcher` and `/best-of-n` when the projected
spend crosses `cost.preflight_threshold_usd`. Mirrors the established
confirm-modal pattern (see `UpdateConfirmScreen`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class PreflightConfirmScreen(ModalScreen[bool]):
    """Ask the user to confirm a projected-cost burst. Dismisses with True/False."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "confirm", "Run", show=False, priority=True),
        Binding("n,escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    PreflightConfirmScreen {
        align: center middle;
    }

    PreflightConfirmScreen > Vertical {
        width: 72;
        height: auto;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
    }

    PreflightConfirmScreen .preflight-title {
        text-style: bold;
        margin-bottom: 1;
    }

    PreflightConfirmScreen .preflight-help {
        text-align: center;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    """

    def __init__(self, name: str, lines: list[str]) -> None:
        """Store what is shown.

        Args:
            name: The command about to run (e.g. `/best-of-n`).
            lines: Projection and cap lines from `cost_controller.preflight_message`.
        """
        super().__init__()
        self._name = name
        self._lines = list(lines)

    def compose(self) -> ComposeResult:
        """Compose the confirmation dialog.

        Yields:
            Widgets for the pre-flight prompt.
        """
        with Vertical(id="preflight-confirm"):
            yield Static(f"Cost pre-flight for {self._name}", classes="preflight-title")
            for line in self._lines:
                yield Static(f"  {line}")
            yield Static(
                "y / Enter to run, n / Esc to cancel", classes="preflight-help"
            )

    def action_confirm(self) -> None:
        """Run the session."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Cancel the session."""
        self.dismiss(False)
