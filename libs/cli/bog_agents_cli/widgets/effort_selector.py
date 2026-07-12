"""Interactive reasoning-effort picker modal for the `/effort` command.

A small `ModalScreen` listing only the effort levels the *active model* accepts.
For a reasoning model that is its native supported set (from
`reasoning_effort.supported_efforts_for_model`); for a non-reasoning model it is
the legacy `low/medium/high/max` preset vocabulary. The currently selected level
and the provider default (when known) are marked. Enter applies the highlighted
level, Esc cancels.

The screen returns the chosen effort label (`str`) on Enter, or `None` on
cancel, via `dismiss`. It carries no config I/O and never mutates app state, so
it is straightforward to test in isolation — the caller applies the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

from bog_agents_cli.reasoning_effort import (
    EFFORT_DESCRIPTIONS,
    default_effort_for_model,
    effort_levels_for_model,
    supported_efforts_for_model,
)


class EffortOption(Static):
    """A clickable effort row in the picker."""

    def __init__(
        self, level: str, label: str, index: int, *, classes: str = ""
    ) -> None:
        """Initialize an effort option row.

        Args:
            level: The effort label (`low`, `medium`, ...).
            label: The human-readable row text to render.
            index: Position of this option in the list (for navigation).
            classes: CSS classes for styling.
        """
        super().__init__(label, classes=classes)
        self.effort_level = level
        self.index = index

    class Clicked(Message):
        """Posted when an effort row is clicked."""

        def __init__(self, level: str, index: int) -> None:
            super().__init__()
            self.effort_level = level
            self.index = index

    def on_click(self) -> None:
        """Forward clicks to the screen for selection."""
        self.post_message(self.Clicked(self.effort_level, self.index))


class EffortSelectorScreen(ModalScreen[str | None]):
    """Full-screen modal for choosing a reasoning-effort level.

    Lists only the levels valid for the active model; returns the chosen label
    on Enter, or `None` on cancel. The caller applies + persists the result.
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
    EffortSelectorScreen {
        align: center middle;
    }

    EffortSelectorScreen > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }

    EffortSelectorScreen .effort-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    EffortSelectorScreen .effort-selector-subtitle {
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }

    EffortSelectorScreen .effort-list {
        height: auto;
        max-height: 18;
    }

    EffortSelectorScreen .effort-option {
        height: auto;
        padding: 0 1;
    }

    EffortSelectorScreen .effort-option:hover {
        background: $surface-lighten-1;
    }

    EffortSelectorScreen .effort-option-selected {
        background: $primary;
        color: $text;
        text-style: bold;
    }

    EffortSelectorScreen .effort-selector-help {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, model_spec: str | None, current: str | None = None) -> None:
        """Initialize the effort picker.

        Args:
            model_spec: `provider:model` spec for the active model. Determines
                which effort levels are offered.
            current: The currently selected effort level to highlight.
        """
        super().__init__()
        self._model_spec = model_spec
        self._levels = list(effort_levels_for_model(model_spec))
        self._native = bool(supported_efforts_for_model(model_spec))
        self._default = default_effort_for_model(model_spec)
        self._current = current
        self._selected_index = 0
        for i, level in enumerate(self._levels):
            if level == current:
                self._selected_index = i
                break

    def compose(self) -> ComposeResult:
        """Compose the picker layout.

        Yields:
            Widgets for the effort selector UI.
        """
        with Vertical():
            yield Static("Reasoning Effort", classes="effort-selector-title")
            if self._native:
                subtitle = "Native reasoning levels for this model"
            else:
                subtitle = "No native reasoning knob — token/temperature presets"
            yield Static(subtitle, classes="effort-selector-subtitle")
            with VerticalScroll(classes="effort-list"):
                for i, level in enumerate(self._levels):
                    classes = "effort-option"
                    if i == self._selected_index:
                        classes += " effort-option-selected"
                    yield EffortOption(
                        level, self._row_label(level), i, classes=classes
                    )
            yield Static(
                "↑/↓ move • Enter apply • Esc cancel",
                classes="effort-selector-help",
            )

    def _row_label(self, level: str) -> str:
        """Build the display label for an effort row.

        Args:
            level: The effort label.

        Returns:
            Rich-markup row text, marking the current level and provider default.
        """
        markers: list[str] = []
        if level == self._current:
            markers.append("current")
        if level == self._default:
            markers.append("default")
        suffix = f" [dim]({', '.join(markers)})[/dim]" if markers else ""
        desc = EFFORT_DESCRIPTIONS.get(level, "")
        return f"[bold]{level}[/bold]{suffix}\n  [dim]{desc}[/dim]"

    def _repaint(self) -> None:
        """Re-apply selection styling across all rows."""
        for option in self.query(EffortOption):
            if option.index == self._selected_index:
                option.add_class("effort-option-selected")
            else:
                option.remove_class("effort-option-selected")

    def _move(self, delta: int) -> None:
        """Move the highlight by `delta`, wrapping.

        Args:
            delta: -1 for up, +1 for down.
        """
        if not self._levels:
            return
        count = len(self._levels)
        self._selected_index = (self._selected_index + delta) % count
        self._repaint()
        for option in self.query(EffortOption):
            if option.index == self._selected_index:
                option.scroll_visible(animate=False)

    def action_move_up(self) -> None:
        """Highlight the previous level."""
        self._move(-1)

    def action_move_down(self) -> None:
        """Highlight the next level."""
        self._move(1)

    def on_effort_option_clicked(self, event: EffortOption.Clicked) -> None:
        """Apply the clicked level immediately.

        Args:
            event: The click event carrying the level and index.
        """
        self._selected_index = event.index
        self.dismiss(event.effort_level)

    def action_select(self) -> None:
        """Apply the highlighted level (caller applies it)."""
        if not self._levels:
            self.dismiss(None)
            return
        self.dismiss(self._levels[self._selected_index])

    def action_cancel(self) -> None:
        """Dismiss without a choice."""
        self.dismiss(None)
