"""Confirmation modal for the `/update` command.

Shows the user exactly what they have and what will be downloaded, then asks
for a yes/no before anything is run. Mirrors the established confirm-modal
pattern (see `DeleteThreadConfirmScreen`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class UpdateConfirmScreen(ModalScreen[bool]):
    """Ask the user to confirm a CLI upgrade. Dismisses with True/False."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "confirm", "Update", show=False, priority=True),
        Binding("n,escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    UpdateConfirmScreen {
        align: center middle;
    }

    UpdateConfirmScreen > Vertical {
        width: 64;
        height: auto;
        background: $surface;
        border: solid $accent;
        padding: 1 2;
    }

    UpdateConfirmScreen .update-confirm-title {
        text-style: bold;
        margin-bottom: 1;
    }

    UpdateConfirmScreen .update-confirm-help {
        text-align: center;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        current: str | None,
        latest: str | None,
        method_label: str,
        display_command: str,
    ) -> None:
        """Store the details shown to the user.

        Args:
            current: Currently installed version.
            latest: Version that will be downloaded.
            method_label: Human label for the install method (e.g. "uv tool").
            display_command: The command that will be run.
        """
        super().__init__()
        self._current = current or "unknown"
        self._latest = latest or "unknown"
        self._method_label = method_label
        self._display_command = display_command

    def compose(self) -> ComposeResult:
        """Compose the confirmation dialog.

        Yields:
            Widgets for the update confirmation prompt.
        """
        with Vertical(id="update-confirm"):
            yield Static("Update bog-agents-cli?", classes="update-confirm-title")
            yield Static(f"  Installed:  {self._current}")
            yield Static(f"  Available:  {self._latest}")
            yield Static(f"  Method:     {self._method_label}")
            yield Static(f"  Command:    {self._display_command}")
            yield Static(
                "y / Enter to update, n / Esc to cancel",
                classes="update-confirm-help",
            )

    def action_confirm(self) -> None:
        """Confirm the update."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Cancel the update."""
        self.dismiss(False)
