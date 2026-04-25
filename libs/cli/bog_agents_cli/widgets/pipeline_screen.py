"""Pipeline picker and runner modal screen.

Displayed by the ``/pipeline`` slash command.  Supports fuzzy search,
variable collection, step preview, and pipeline execution.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.fuzzy import Matcher
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult

    from bog_agents_cli.pipeline import Pipeline

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type returned to the caller
# ---------------------------------------------------------------------------


@dataclass
class PipelineRunRequest:
    """Request to run a pipeline returned by :class:`PipelineScreen`."""

    pipeline: Pipeline
    variable_values: dict[str, str]


# ---------------------------------------------------------------------------
# Variable-collection dialog (reused from prompt library pattern)
# ---------------------------------------------------------------------------


class PipelineVariableScreen(ModalScreen["dict[str, str] | None"]):
    """Collect top-level pipeline variable values from the user."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PipelineVariableScreen {
        align: center middle;
    }
    PipelineVariableScreen > Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }
    PipelineVariableScreen #pv-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    PipelineVariableScreen Input {
        margin-bottom: 1;
    }
    PipelineVariableScreen Label {
        color: $text-muted;
    }
    PipelineVariableScreen #pv-buttons {
        height: 3;
        margin-top: 1;
    }
    """

    def __init__(self, pipeline: Pipeline) -> None:
        super().__init__()
        self._pipeline = pipeline

    def compose(self) -> ComposeResult:
        """Build the variable collection form.

        Yields:
            Textual widgets for the screen layout.
        """
        with Vertical():
            yield Label(f"Variables for: {self._pipeline.name}", id="pv-title")
            with VerticalScroll():
                for var in self._pipeline.variables:
                    yield Label(f"{{{{ {var} }}}}")
                    yield Input(placeholder=f"Enter {var}…", id=f"pv-{var}")
            with Horizontal(id="pv-buttons"):
                yield Button("Run Pipeline", id="pv-run", variant="primary")
                yield Button("Cancel", id="pv-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Run Pipeline / Cancel button presses."""
        if event.button.id == "pv-cancel":
            self.dismiss(None)
            return
        values: dict[str, str] = {}
        for var in self._pipeline.variables:
            inp = self.query_one(f"#pv-{var}", Input)
            values[var] = inp.value
        self.dismiss(values)

    def action_cancel(self) -> None:
        """Cancel and close without returning values."""
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main pipeline screen
# ---------------------------------------------------------------------------


class PipelineScreen(ModalScreen["PipelineRunRequest | None"]):
    """Full-screen pipeline picker with fuzzy search and step preview."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up,k", "move_up", "Up", show=False, priority=True),
        Binding("down,j", "move_down", "Down", show=False, priority=True),
        Binding("enter", "select", "Run", show=True, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PipelineScreen {
        align: center middle;
    }
    PipelineScreen > Vertical {
        width: 90;
        max-width: 95%;
        height: 85%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }
    PipelineScreen #pl-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    PipelineScreen #pl-search {
        margin-bottom: 1;
    }
    PipelineScreen #pl-list {
        height: 1fr;
        border: tall $surface;
    }
    PipelineScreen .pipeline-row {
        height: 3;
        padding: 0 1;
    }
    PipelineScreen .pipeline-row.--highlight {
        background: $primary 20%;
    }
    PipelineScreen #pl-preview {
        height: 10;
        border: tall $surface;
        padding: 0 1;
        color: $text-muted;
        margin-top: 1;
    }
    PipelineScreen #pl-hint {
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    PipelineScreen #pl-empty {
        color: $text-muted;
        text-style: italic;
        padding: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._pipelines: list[Any] = []
        self._filtered: list[Any] = []
        self._selected_index: int = 0
        self._row_widgets: list[Static] = []

    def compose(self) -> ComposeResult:  # noqa: PLR6301
        """Build the pipeline picker UI.

        Yields:
            Textual widgets for the screen layout.
        """
        with Vertical():
            yield Label("Pipelines", id="pl-title")
            yield Input(placeholder="Search pipelines…", id="pl-search")
            with VerticalScroll(id="pl-list"):
                yield Static("Loading…", id="pl-loading")
            yield Static("", id="pl-preview")
            yield Static(
                "Enter: run · Esc: cancel",
                id="pl-hint",
            )

    def on_mount(self) -> None:
        """Load pipelines and focus search on mount."""
        self._reload()
        self.query_one("#pl-search", Input).focus()

    def _reload(self) -> None:
        from bog_agents_cli.pipeline import list_pipelines

        try:
            self._pipelines = sorted(list_pipelines(), key=lambda p: p.name)
        except Exception:
            self._pipelines = []
        self._apply_filter(self.query_one("#pl-search", Input).value)

    def _apply_filter(self, query: str) -> None:
        if query:
            matcher = Matcher(query)
            self._filtered = [
                p
                for p in self._pipelines
                if matcher.match(p.name) or matcher.match(p.description)
            ]
        else:
            self._filtered = list(self._pipelines)
        self._selected_index = 0
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        container = self.query_one("#pl-list", VerticalScroll)
        container.remove_children()
        self._row_widgets = []

        if not self._filtered:
            empty_text = (
                "No pipelines found.\n\n"
                "Create a YAML file in ~/.bog-agents/pipelines/\n"
                "See /help pipeline for the schema."
            )
            container.mount(Static(empty_text, id="pl-empty"))
            self._update_preview()
            return

        for i, pipeline in enumerate(self._filtered):
            sched = f"  [dim]{pipeline.schedule}[/dim]" if pipeline.schedule else ""
            row = Static(
                f"[bold]{pipeline.name}[/bold]  "
                f"[dim]{pipeline.description or ''}[/dim]{sched}",
                classes="pipeline-row"
                + (" --highlight" if i == self._selected_index else ""),
            )
            self._row_widgets.append(row)
            container.mount(row)

        self._update_preview()

    def _update_highlight(self) -> None:
        for i, row in enumerate(self._row_widgets):
            if i == self._selected_index:
                row.add_class("--highlight")
            else:
                row.remove_class("--highlight")
        self._update_preview()
        if self._row_widgets and 0 <= self._selected_index < len(self._row_widgets):
            self._row_widgets[self._selected_index].scroll_visible()

    def _update_preview(self) -> None:
        preview = self.query_one("#pl-preview", Static)
        if not self._filtered or self._selected_index >= len(self._filtered):
            preview.update("")
            return
        p = self._filtered[self._selected_index]
        lines = []
        for i, step in enumerate(p.steps[:6]):
            icon = {"prompt": "◆", "message": "▸", "slash": "/"}.get(step.type, "•")
            label = step.name or step.text[:40] or step.command or step.id
            lines.append(f"  {icon} [{i + 1}] [dim]{step.id}:[/dim] {label}")
        if len(p.steps) > 6:
            lines.append(f"  … {len(p.steps) - 6} more steps")
        if p.variables:
            lines.append(f"\n  [dim]vars: {', '.join(p.variables)}[/dim]")
        if p.schedule:
            lines.append(f"  [dim]schedule: {p.schedule}[/dim]")
        preview.update("\n".join(lines))

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-filter the list when the search input changes."""
        if event.input.id == "pl-search":
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the selected pipeline when Enter is pressed in the search box."""
        if event.input.id == "pl-search":
            self.action_select()

    def action_move_up(self) -> None:
        """Move selection up one row."""
        if self._filtered and self._selected_index > 0:
            self._selected_index -= 1
            self._update_highlight()

    def action_move_down(self) -> None:
        """Move selection down one row."""
        if self._filtered and self._selected_index < len(self._filtered) - 1:
            self._selected_index += 1
            self._update_highlight()

    def action_select(self) -> None:
        """Run the currently highlighted pipeline, collecting variables if needed."""
        if not self._filtered or self._selected_index >= len(self._filtered):
            return
        pipeline = self._filtered[self._selected_index]
        if pipeline.variables:
            self.app.push_screen(
                PipelineVariableScreen(pipeline),
                self._on_variables(pipeline),
            )
        else:
            self.dismiss(PipelineRunRequest(pipeline=pipeline, variable_values={}))

    def _on_variables(
        self, pipeline: Pipeline
    ) -> Callable[[dict[str, str] | None], None]:
        def handler(values: dict[str, str] | None) -> None:
            if values is None:
                return
            self.dismiss(PipelineRunRequest(pipeline=pipeline, variable_values=values))

        return handler

    def action_cancel(self) -> None:
        """Cancel and close the pipeline picker."""
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        """Handle raw key events for vim-style navigation."""
        if event.key in ("up", "k"):
            self.action_move_up()
            event.prevent_default()
        elif event.key in ("down", "j"):
            self.action_move_down()
            event.prevent_default()
