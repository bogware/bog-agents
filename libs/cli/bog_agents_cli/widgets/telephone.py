"""Confirmation widget for the ``/telephone`` rewrite flow.

Shows the rewritten prompt and offers three actions:

* **Submit** — send the rewrite to the agent as the next user message.
* **Redo**   — re-run the rewriter, keeping the same original input.
* **Ditch**  — abandon the rewrite, leave the user's input unchanged.

Mirrors :class:`bog_agents_cli.widgets.approval.ApprovalMenu` styling so
the experience feels consistent with the rest of the modal flow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from rich.markup import escape as escape_markup
from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

if TYPE_CHECKING:
    import asyncio

    from textual.app import ComposeResult

from bog_agents_cli.config import (
    CharsetMode,
    _detect_charset_mode,
    get_glyphs,
)


class TelephoneMenu(Container):
    """Modal confirmation for a /telephone rewrite."""

    can_focus = True
    can_focus_children = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("j", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("1", "select_submit", "Submit", show=False),
        Binding("y", "select_submit", "Submit", show=False),
        Binding("2", "select_redo", "Redo", show=False),
        Binding("r", "select_redo", "Redo", show=False),
        Binding("3", "select_ditch", "Ditch", show=False),
        Binding("n", "select_ditch", "Ditch", show=False),
        Binding("escape", "select_ditch", "Ditch", show=False),
    ]

    class Decided(Message):
        """User picked one of the three actions."""

        def __init__(self, decision: str) -> None:
            """Initialize.

            Args:
                decision: One of ``"submit"``, ``"redo"``, ``"ditch"``.
            """
            super().__init__()
            self.decision = decision

    def __init__(
        self,
        original_prompt: str,
        rewritten_prompt: str,
        id: str | None = None,  # noqa: A002  # Textual widget convention
        **kwargs: Any,
    ) -> None:
        """Initialize.

        Args:
            original_prompt: The user's untouched input, shown for diff context.
            rewritten_prompt: The rewriter's output that's pending approval.
            id: Optional widget id.
            **kwargs: Forwarded to ``Container``.
        """
        super().__init__(id=id or "telephone-menu", classes="approval-menu", **kwargs)
        self._original = original_prompt
        self._rewritten = rewritten_prompt
        self._selected = 0
        self._option_widgets: list[Static] = []
        self._future: asyncio.Future[str] | None = None

    def set_future(self, future: asyncio.Future[str]) -> None:
        """Wire a future the caller can ``await`` for the user's decision."""
        self._future = future

    def compose(self) -> ComposeResult:
        """Render header, both prompt panes, and three options.

        Yields:
            Header static, scrollable container for the original/rewritten
            text, separator, three option widgets, and a help line.
        """
        yield Static(
            ">>> /telephone — rewritten prompt <<<",
            classes="approval-title",
        )
        with VerticalScroll(classes="tool-info-scroll"):
            container = Vertical(classes="tool-info-container")
            yield container

        glyphs = get_glyphs()
        yield Static(glyphs.box_horizontal * 40, classes="approval-separator")

        with Container(classes="approval-options-container"):
            for _ in range(3):
                widget = Static("", classes="approval-option")
                self._option_widgets.append(widget)
                yield widget

        help_text = (
            f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate {glyphs.bullet} "
            f"Enter select {glyphs.bullet} y/r/n quick keys {glyphs.bullet} Esc ditch"
        )
        yield Static(help_text, classes="approval-help")

        # Compose populates the structure; on_mount fills in dynamic content.
        self._tool_info_container = container

    async def on_mount(self) -> None:
        """Fill in original/rewritten panes and select the default option."""
        if _detect_charset_mode() == CharsetMode.ASCII:
            self.styles.border = ("ascii", "yellow")

        await self._tool_info_container.mount(
            Static("[bold]Original[/bold]"),
            Static(
                f"[dim]{escape_markup(self._original)}[/dim]",
                classes="approval-description",
            ),
            Static(""),
            Static("[bold]Rewritten[/bold]"),
            Static(
                f"[bold #22c55e]{escape_markup(self._rewritten)}[/bold #22c55e]",
                classes="approval-description",
            ),
        )
        self._update_options()
        self.focus()

    def _update_options(self) -> None:
        """Refresh the three option labels and selection styling."""
        options = (
            "1. Submit (y) — send to agent",
            "2. Redo (r) — re-run rewriter",
            "3. Ditch (n) — discard rewrite",
        )
        glyphs = get_glyphs()
        for i, (text, widget) in enumerate(
            zip(options, self._option_widgets, strict=True)
        ):
            cursor = f"{glyphs.cursor} " if i == self._selected else "  "
            widget.update(f"{cursor}{text}")
            widget.remove_class("approval-option-selected")
            if i == self._selected:
                widget.add_class("approval-option-selected")

    def action_move_up(self) -> None:
        """Cycle to the previous option."""
        self._selected = (self._selected - 1) % 3
        self._update_options()

    def action_move_down(self) -> None:
        """Cycle to the next option."""
        self._selected = (self._selected + 1) % 3
        self._update_options()

    def action_select(self) -> None:
        """Apply the currently highlighted option."""
        self._dispatch(["submit", "redo", "ditch"][self._selected])

    def action_select_submit(self) -> None:
        """Pick Submit and dispatch."""
        self._selected = 0
        self._update_options()
        self._dispatch("submit")

    def action_select_redo(self) -> None:
        """Pick Redo and dispatch."""
        self._selected = 1
        self._update_options()
        self._dispatch("redo")

    def action_select_ditch(self) -> None:
        """Pick Ditch and dispatch."""
        self._selected = 2
        self._update_options()
        self._dispatch("ditch")

    def _dispatch(self, decision: str) -> None:
        """Resolve the future and post a Decided message."""
        if self._future and not self._future.done():
            self._future.set_result(decision)
        self.post_message(self.Decided(decision))


__all__ = ["TelephoneMenu"]
