"""Interactive theme picker modal for the `/theme` command.

A small `ModalScreen` listing every registered theme (built-in + user). The
active theme is highlighted; Enter applies + persists the highlighted theme,
Esc cancels. Highlighting a theme live-previews it by setting `app.theme`
directly — cancelling restores the theme that was active on open, so a
preview never leaks into the persisted preference.

The screen returns the chosen theme name (`str`) on Enter, or `None` on
cancel, via `dismiss`. Persistence is handled by the caller so this widget
stays free of config I/O and easy to test.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

from bog_agents_cli.theme import all_theme_specs

logger = logging.getLogger(__name__)


class ThemeOption(Static):
    """A clickable theme row in the picker."""

    def __init__(self, name: str, label: str, index: int, *, classes: str = "") -> None:
        """Initialize a theme option row.

        Args:
            name: The theme name (registry key / `App.theme` value).
            label: The human-readable label to render.
            index: Position of this option in the list (for navigation).
            classes: CSS classes for styling.
        """
        super().__init__(label, classes=classes)
        self.theme_name = name
        self.index = index

    class Clicked(Message):
        """Posted when a theme row is clicked."""

        def __init__(self, name: str, index: int) -> None:
            super().__init__()
            self.theme_name = name
            self.index = index

    def on_click(self) -> None:
        """Forward clicks to the screen for selection."""
        self.post_message(self.Clicked(self.theme_name, self.index))


class ThemeSelectorScreen(ModalScreen[str | None]):
    """Full-screen modal for choosing a theme.

    Returns the chosen theme name on Enter, or `None` on cancel. Highlighting
    a row live-previews it; cancelling restores the theme active on open.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("k", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("j", "move_down", "Down", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    ThemeSelectorScreen {
        align: center middle;
    }

    ThemeSelectorScreen > Vertical {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }

    ThemeSelectorScreen .theme-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    ThemeSelectorScreen .theme-list {
        height: auto;
        max-height: 20;
    }

    ThemeSelectorScreen .theme-option {
        height: 1;
        padding: 0 1;
    }

    ThemeSelectorScreen .theme-option:hover {
        background: $highlight-soft;
    }

    ThemeSelectorScreen .theme-option-selected {
        background: $highlight;
        color: $primary-lighten-1;
        text-style: bold;
    }

    ThemeSelectorScreen .theme-selector-help {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, current: str | None = None) -> None:
        """Initialize the theme picker.

        Args:
            current: The active theme name to highlight and restore on cancel.
        """
        super().__init__()
        self._themes = [(spec.name, spec.label) for spec in all_theme_specs()]
        self._original = current
        self._selected_index = 0
        for i, (name, _label) in enumerate(self._themes):
            if name == current:
                self._selected_index = i
                break

    def compose(self) -> ComposeResult:
        """Compose the picker layout.

        Yields:
            Widgets for the theme selector UI.
        """
        with Vertical():
            yield Static("Select Theme", classes="theme-selector-title")
            with VerticalScroll(classes="theme-list"):
                for i, (name, label) in enumerate(self._themes):
                    classes = "theme-option"
                    if i == self._selected_index:
                        classes += " theme-option-selected"
                    yield ThemeOption(
                        name, self._row_label(name, label), i, classes=classes
                    )
            yield Static(
                "↑/↓ preview • Enter apply • Esc cancel",
                classes="theme-selector-help",
            )

    def _row_label(self, name: str, label: str) -> str:
        """Build the display label for a theme row.

        Args:
            name: Theme name.
            label: Theme display label.

        Returns:
            Rich-markup row text, marking the currently active theme.
        """
        suffix = " [dim](current)[/dim]" if name == self._original else ""
        return f"{label}  [dim]{name}[/dim]{suffix}"

    def on_mount(self) -> None:
        """Preview the initially selected theme on open."""
        self._preview_selected()

    def _preview_selected(self) -> None:
        """Set `app.theme` to the highlighted theme (live preview)."""
        if not self._themes:
            return
        name = self._themes[self._selected_index][0]
        try:
            self.app.theme = name
        except Exception:
            logger.debug("Could not preview theme %r", name, exc_info=True)

    def _repaint(self) -> None:
        """Re-apply selection styling across all rows."""
        for option in self.query(ThemeOption):
            if option.index == self._selected_index:
                option.add_class("theme-option-selected")
            else:
                option.remove_class("theme-option-selected")

    def _move(self, delta: int) -> None:
        """Move the highlight by `delta`, wrapping, and preview it.

        Args:
            delta: -1 for up, +1 for down.
        """
        if not self._themes:
            return
        count = len(self._themes)
        self._selected_index = (self._selected_index + delta) % count
        self._repaint()
        self._preview_selected()
        for option in self.query(ThemeOption):
            if option.index == self._selected_index:
                option.scroll_visible(animate=False)

    def action_move_up(self) -> None:
        """Highlight the previous theme."""
        self._move(-1)

    def action_move_down(self) -> None:
        """Highlight the next theme."""
        self._move(1)

    def on_theme_option_clicked(self, event: ThemeOption.Clicked) -> None:
        """Apply the clicked theme immediately.

        Args:
            event: The click event carrying the theme name and index.
        """
        self._selected_index = event.index
        self.dismiss(event.theme_name)

    def action_select(self) -> None:
        """Apply the highlighted theme (caller persists it)."""
        if not self._themes:
            self.dismiss(None)
            return
        self.dismiss(self._themes[self._selected_index][0])

    def action_cancel(self) -> None:
        """Restore the theme active on open and dismiss without a choice."""
        if self._original is not None:
            try:
                self.app.theme = self._original
            except Exception:
                logger.debug(
                    "Could not restore theme %r", self._original, exc_info=True
                )
        self.dismiss(None)
