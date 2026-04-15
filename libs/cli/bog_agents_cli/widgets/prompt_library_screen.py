"""Prompt library picker and editor modal screen.

Displayed by the ``/prompt`` slash command.  Supports fuzzy search, CRUD
operations, variable substitution, and runs a selected prompt directly.
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
from textual.widgets import Button, Input, Label, Static, TextArea

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult

    from bog_agents_cli.prompt_library import PromptEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type returned to the caller
# ---------------------------------------------------------------------------


@dataclass
class PromptResult:
    """Result from the prompt library screen."""

    text: str  # Fully rendered prompt ready to submit


# ---------------------------------------------------------------------------
# Variable-collection dialog
# ---------------------------------------------------------------------------


class VariableInputScreen(ModalScreen["dict[str, str] | None"]):
    """Ask the user to fill in ``{{variable}}`` placeholders.

    Returned value is a dict of ``{var_name: value}`` or ``None`` on cancel.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    VariableInputScreen {
        align: center middle;
    }
    VariableInputScreen > Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }
    VariableInputScreen Label {
        margin: 0 0 0 0;
        color: $text-muted;
    }
    VariableInputScreen Input {
        margin-bottom: 1;
    }
    VariableInputScreen #var-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    VariableInputScreen #var-buttons {
        height: 3;
        margin-top: 1;
    }
    """

    def __init__(self, prompt_name: str, variables: list[str]) -> None:
        super().__init__()
        self._prompt_name = prompt_name
        self._variables = variables

    def compose(self) -> ComposeResult:
        """Build the variable input form.

        Yields:
            Textual widgets for the screen layout.
        """
        with Vertical():
            yield Label(f"Variables for: {self._prompt_name}", id="var-title")
            with VerticalScroll():
                for var in self._variables:
                    yield Label(f"{{{{  {var}  }}}}")
                    yield Input(placeholder=f"Enter {var}…", id=f"var-{var}")
            with Horizontal(id="var-buttons"):
                yield Button("Run Prompt", id="var-run", variant="primary")
                yield Button("Cancel", id="var-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Run/Cancel button presses."""
        if event.button.id == "var-cancel":
            self.dismiss(None)
            return
        values: dict[str, str] = {}
        for var in self._variables:
            inp = self.query_one(f"#var-{var}", Input)
            values[var] = inp.value
        self.dismiss(values)

    def action_cancel(self) -> None:
        """Cancel and close without returning values."""
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Prompt edit/create dialog
# ---------------------------------------------------------------------------


class PromptEditScreen(ModalScreen["PromptEntry | None"]):
    """Create or edit a prompt entry."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PromptEditScreen {
        align: center middle;
    }
    PromptEditScreen > Vertical {
        width: 80;
        max-width: 95%;
        height: 85%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }
    PromptEditScreen #edit-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    PromptEditScreen Label {
        color: $text-muted;
        margin-top: 1;
    }
    PromptEditScreen Input {
        margin-bottom: 1;
    }
    PromptEditScreen TextArea {
        height: 15;
        margin-bottom: 1;
    }
    PromptEditScreen #edit-hint {
        color: $text-muted;
        text-style: italic;
        margin-bottom: 1;
    }
    PromptEditScreen #edit-buttons {
        height: 3;
        margin-top: 1;
    }
    """

    def __init__(self, entry: PromptEntry | None = None) -> None:
        super().__init__()
        self._entry = entry  # None means creating a new prompt

    def compose(self) -> ComposeResult:
        """Build the prompt edit form.

        Yields:
            Textual widgets for the screen layout.
        """
        title = "Edit Prompt" if self._entry else "New Prompt"
        with Vertical():
            yield Label(title, id="edit-title")
            yield Label("Name")
            yield Input(
                value=self._entry.name if self._entry else "",
                placeholder="my-prompt",
                id="edit-name",
            )
            yield Label("Description (optional)")
            yield Input(
                value=self._entry.description if self._entry else "",
                placeholder="What does this prompt do?",
                id="edit-desc",
            )
            yield Label("Body  (use {{variable}} for placeholders)")
            yield TextArea(
                text=self._entry.body if self._entry else "",
                id="edit-body",
                language="markdown",
                show_line_numbers=True,
            )
            yield Static(
                "Tip: Ctrl+S saves · Escape cancels · {{var}} creates a variable",
                id="edit-hint",
            )
            with Horizontal(id="edit-buttons"):
                yield Button("Save", id="edit-save", variant="primary")
                yield Button("Cancel", id="edit-cancel")

    def action_save(self) -> None:
        """Trigger save via keyboard shortcut."""
        self._do_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Save/Cancel button presses."""
        if event.button.id == "edit-cancel":
            self.dismiss(None)
        elif event.button.id == "edit-save":
            self._do_save()

    def action_cancel(self) -> None:
        """Cancel and close without saving."""
        self.dismiss(None)

    def _do_save(self) -> None:
        from bog_agents_cli.prompt_library import PromptEntry

        name = self.query_one("#edit-name", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="error")
            return
        body = self.query_one("#edit-body", TextArea).text.strip()
        if not body:
            self.notify("Body is required", severity="error")
            return
        desc = self.query_one("#edit-desc", Input).value.strip()
        entry = PromptEntry(name=name, body=body, description=desc)
        self.dismiss(entry)


# ---------------------------------------------------------------------------
# Main prompt library screen
# ---------------------------------------------------------------------------


class PromptLibraryScreen(ModalScreen["PromptResult | None"]):
    """Full-screen prompt library picker.

    Pressing Enter on a prompt either runs it directly (if it has no
    variables) or opens the variable-collection dialog first.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up,k", "move_up", "Up", show=False, priority=True),
        Binding("down,j", "move_down", "Down", show=False, priority=True),
        Binding("enter", "select", "Run Prompt", show=True, priority=True),
        Binding("ctrl+n", "new_prompt", "New", show=True),
        Binding("ctrl+e", "edit_prompt", "Edit", show=True),
        Binding("ctrl+d", "delete_prompt", "Delete", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PromptLibraryScreen {
        align: center middle;
    }
    PromptLibraryScreen > Vertical {
        width: 90;
        max-width: 95%;
        height: 85%;
        background: $surface-darken-1;
        border: round $primary;
        padding: 1 2;
    }
    PromptLibraryScreen #lib-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    PromptLibraryScreen #lib-search {
        margin-bottom: 1;
    }
    PromptLibraryScreen #lib-list {
        height: 1fr;
        border: tall $surface;
    }
    PromptLibraryScreen .prompt-row {
        height: 3;
        padding: 0 1;
    }
    PromptLibraryScreen .prompt-row.--highlight {
        background: $primary 20%;
    }
    PromptLibraryScreen .prompt-name {
        color: $text;
        text-style: bold;
        width: 25;
    }
    PromptLibraryScreen .prompt-desc {
        color: $text-muted;
        text-style: italic;
    }
    PromptLibraryScreen #lib-preview {
        height: 8;
        border: tall $surface;
        padding: 0 1;
        color: $text-muted;
        margin-top: 1;
    }
    PromptLibraryScreen #lib-hint {
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
    }
    PromptLibraryScreen #lib-empty {
        color: $text-muted;
        text-style: italic;
        padding: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[Any] = []
        self._filtered: list[Any] = []
        self._selected_index: int = 0
        self._row_widgets: list[Static] = []

    def compose(self) -> ComposeResult:  # noqa: PLR6301
        """Build the prompt library picker UI.

        Yields:
            Textual widgets for the screen layout.
        """
        with Vertical():
            yield Label("Prompt Library", id="lib-title")
            yield Input(placeholder="Search prompts…", id="lib-search")
            with VerticalScroll(id="lib-list"):
                yield Static("Loading…", id="lib-loading")
            yield Static("", id="lib-preview")
            yield Static(
                "Enter: run · Ctrl+N: new · Ctrl+E: edit · Ctrl+D: delete · Esc: cancel",
                id="lib-hint",
            )

    def on_mount(self) -> None:
        """Load entries and focus search on mount."""
        self._reload_entries()
        self.query_one("#lib-search", Input).focus()

    def _reload_entries(self) -> None:
        from bog_agents_cli.prompt_library import load_library

        try:
            lib = load_library()
            self._entries = sorted(lib.values(), key=lambda e: e.name)
        except Exception:
            self._entries = []
        self._apply_filter(self.query_one("#lib-search", Input).value)

    def _apply_filter(self, query: str) -> None:
        if query:
            matcher = Matcher(query)
            self._filtered = [
                e
                for e in self._entries
                if matcher.match(e.name) or matcher.match(e.description)
            ]
        else:
            self._filtered = list(self._entries)
        self._selected_index = 0
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        container = self.query_one("#lib-list", VerticalScroll)
        container.remove_children()
        self._row_widgets = []

        if not self._filtered:
            container.mount(
                Static("No prompts found. Press Ctrl+N to create one.", id="lib-empty")
            )
            self._update_preview()
            return

        for i, entry in enumerate(self._filtered):
            row = Static(
                f"[bold]{entry.name}[/bold]  [dim]{entry.description or ''}[/dim]",
                classes="prompt-row"
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
        preview = self.query_one("#lib-preview", Static)
        if not self._filtered or self._selected_index >= len(self._filtered):
            preview.update("")
            return
        entry = self._filtered[self._selected_index]
        snippet = entry.body[:200].replace("\n", " ↵ ")
        if len(entry.body) > 200:
            snippet += "…"
        vars_str = f"  vars: {', '.join(entry.variables)}" if entry.variables else ""
        preview.update(f"[dim]{snippet}[/dim]{vars_str}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-filter the list when the search input changes."""
        if event.input.id == "lib-search":
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the selected prompt when Enter is pressed in the search box."""
        if event.input.id == "lib-search":
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
        """Run the currently highlighted prompt, collecting variables if needed."""
        if not self._filtered or self._selected_index >= len(self._filtered):
            return
        entry = self._filtered[self._selected_index]
        if entry.variables:
            self.app.push_screen(
                VariableInputScreen(entry.name, entry.variables),
                self._on_variables_collected(entry),
            )
        else:
            self.dismiss(PromptResult(entry.body))

    def _on_variables_collected(
        self, entry: PromptEntry
    ) -> Callable[[dict[str, str] | None], None]:
        def handler(values: dict[str, str] | None) -> None:
            if values is None:
                return  # User cancelled variable entry
            try:
                rendered = entry.render(values)
                self.dismiss(PromptResult(rendered))
            except KeyError as exc:
                self.notify(f"Missing variable: {exc}", severity="error")

        return handler

    def action_new_prompt(self) -> None:
        """Open the edit dialog to create a new prompt."""
        self.app.push_screen(PromptEditScreen(), self._on_edit_result)

    def action_edit_prompt(self) -> None:
        """Open the edit dialog for the currently highlighted prompt."""
        if not self._filtered or self._selected_index >= len(self._filtered):
            return
        entry = self._filtered[self._selected_index]
        self.app.push_screen(PromptEditScreen(entry), self._on_edit_result)

    def _on_edit_result(self, result: PromptEntry | None) -> None:
        if result is None:
            return
        from bog_agents_cli.prompt_library import save_prompt

        try:
            save_prompt(result)
            self.notify(f"Saved prompt '{result.name}'", severity="information")
            self._reload_entries()
        except Exception as exc:
            self.notify(f"Failed to save: {exc}", severity="error")

    def action_delete_prompt(self) -> None:
        """Delete the currently highlighted prompt."""
        if not self._filtered or self._selected_index >= len(self._filtered):
            return
        entry = self._filtered[self._selected_index]
        from bog_agents_cli.prompt_library import delete_prompt

        try:
            delete_prompt(entry.name)
            self.notify(f"Deleted '{entry.name}'", severity="information")
            self._reload_entries()
        except Exception as exc:
            self.notify(f"Failed to delete: {exc}", severity="error")

    def action_cancel(self) -> None:
        """Cancel and close the prompt library screen."""
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        """Handle raw key events for vim-style navigation."""
        if event.key in ("up", "k"):
            self.action_move_up()
            event.prevent_default()
        elif event.key in ("down", "j"):
            self.action_move_down()
            event.prevent_default()
