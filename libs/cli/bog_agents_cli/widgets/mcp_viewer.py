"""Read-only MCP server and tool viewer modal."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.events import (
    Click,  # noqa: TC002 - needed at runtime for Textual event dispatch
)
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from bog_agents_cli.mcp_tools import MCPServerInfo

from bog_agents_cli.config import CharsetMode, _detect_charset_mode, get_glyphs
from bog_agents_cli.unicode_security import (
    sanitize_control_chars,
    strip_dangerous_unicode,
)


def _safe_markup(text: str) -> str:
    """Make server-controlled MCP text safe to embed in Rich markup.

    Applies, in order: `sanitize_control_chars` (strip terminal escape
    sequences / control bytes), `strip_dangerous_unicode` (drop bidi and
    zero-width confusables), then Rich markup escaping. Sanitizing BEFORE
    escaping is deliberate: escaping first can leave an `ESC` byte directly in
    front of a markup token, and stripping that escape afterwards consumes the
    escaping backslash — re-exposing an active tag (e.g. a bare `[/dim]`) that
    raises `MarkupError` at render time. Doing markup escaping last closes that
    hole.

    Args:
        text: Untrusted, server-controlled string.

    Returns:
        Text safe to interpolate into a Rich-rendered `Static`.
    """
    from rich.markup import escape as escape_markup

    return escape_markup(strip_dangerous_unicode(sanitize_control_chars(text)))


def _safe_literal(text: str) -> str:
    """Sanitize server-controlled MCP text for literal `Content` rendering.

    Applies `sanitize_control_chars` (strip terminal escape sequences / control
    bytes) then `strip_dangerous_unicode` (drop bidi and zero-width
    confusables), but — unlike `_safe_markup` — does NOT Rich-escape the result.
    `Content.assemble` treats its text parts as literal and never runs them
    through the markup parser, so escaping is both unnecessary and would surface
    visible backslashes. The escaping still guards the `_safe_markup` path
    (belt-and-suspenders); this variant feeds the preferred `Content` path.

    Args:
        text: Untrusted, server-controlled string.

    Returns:
        Text safe to hand to `Content.assemble` as a literal part.
    """
    return strip_dangerous_unicode(sanitize_control_chars(text))


class MCPToolItem(Static):
    """A selectable tool item in the MCP viewer."""

    def __init__(
        self,
        name: str,
        description: str,
        index: int,
        *,
        classes: str = "",
    ) -> None:
        """Initialize a tool item.

        Args:
            name: Tool name.
            description: Full tool description.
            index: Flat index of this tool in the list.
            classes: CSS classes.
        """
        # Tool name/description are fully server-controlled and untrusted.
        # Preferred render path: `Content.assemble`, which treats text parts as
        # LITERAL and never runs them through the markup parser — so an injected
        # `[bold]`/`[/dim]` can never activate. We still keep `_safe_markup`
        # (Rich-escaped) copies in `tool_name`/`tool_description` as
        # belt-and-suspenders for any markup-string consumer. Both paths first
        # strip terminal escapes/control bytes and bidi/zero-width confusables.
        literal_name = _safe_literal(name)
        literal_description = _safe_literal(description) if description else ""
        # NB: attribute names must avoid `self._name`, which Textual's DOMNode
        # owns for the widget `name` — `super().__init__` would clobber it.
        super().__init__(
            self._format_collapsed(literal_name, literal_description), classes=classes
        )
        self.tool_name = _safe_markup(name)
        self.tool_description = _safe_markup(description) if description else ""
        self._literal_name = literal_name
        self._literal_description = literal_description
        self.index = index
        self._expanded = False

    def _format_collapsed(self, name: str, description: str) -> Content:
        """Build the collapsed (single-line) content.

        Truncates the description with `(...)` if it would overflow the widget
        width. The pieces are handed to `Content.assemble` as literal text with
        an explicit `dim` style span, so server-controlled brackets render
        verbatim rather than as markup. Truncating literal text is safe — there
        is no markup token that a slice could split mid-tag.

        Args:
            name: Sanitized (literal) tool name.
            description: Sanitized (literal) tool description.

        Returns:
            Renderable `Content` for the collapsed row.
        """
        if not description:
            return Content.assemble(f"  {name}")
        prefix_len = 2 + len(name) + 1
        try:
            width = self.size.width
        except (RuntimeError, AttributeError):
            # `self.size` is unavailable before the widget is mounted (e.g. when
            # building the initial content in `__init__`); render untruncated.
            width = 0
        avail = width - prefix_len - 1 if width else 0
        ellipsis = " (...)"
        if avail > 0 and len(description) > avail:
            cut = max(0, avail - len(ellipsis))
            desc_text = description[:cut] + ellipsis
        else:
            desc_text = description
        return Content.assemble(f"  {name} ", (desc_text, "dim"))

    @staticmethod
    def _format_expanded(name: str, description: str) -> Content:
        """Build the expanded (multi-line) content.

        Expects sanitized (literal) `name`/`description`; assembled as literal
        `Content` so untrusted text is never markup-parsed.

        Args:
            name: Sanitized (literal) tool name.
            description: Sanitized (literal) tool description.

        Returns:
            Renderable `Content` with the full description on the next line.
        """
        if description:
            return Content.assemble(
                "  ", (name, "bold"), "\n    ", (description, "dim")
            )
        return Content.assemble("  ", (name, "bold"))

    def toggle_expand(self) -> None:
        """Toggle between collapsed and expanded view."""
        self._expanded = not self._expanded
        if self._expanded:
            content = self._format_expanded(
                self._literal_name, self._literal_description
            )
            self.styles.height = "auto"
        else:
            content = self._format_collapsed(
                self._literal_name, self._literal_description
            )
            self.styles.height = 1
        self.update(content)

    def on_mount(self) -> None:
        """Re-render with correct truncation once width is known."""
        if not self._expanded:
            self.update(
                self._format_collapsed(self._literal_name, self._literal_description)
            )

    def on_resize(self) -> None:
        """Re-truncate when widget width changes."""
        if not self._expanded:
            self.update(
                self._format_collapsed(self._literal_name, self._literal_description)
            )

    def on_click(self, event: Click) -> None:
        """Handle click — select and toggle expand via parent screen.

        Args:
            event: The click event.
        """
        event.stop()
        screen = self.screen
        if isinstance(screen, MCPViewerScreen):
            screen._move_to(self.index)
            self.toggle_expand()


class MCPViewerScreen(ModalScreen[None]):
    """Modal viewer for active MCP servers and their tools.

    Displays servers grouped by name with transport type and tool count.
    Navigate with arrow keys, Enter to expand/collapse tool descriptions,
    Escape to close.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("k", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("j", "move_down", "Down", show=False, priority=True),
        Binding("enter", "toggle_expand", "Expand", show=False, priority=True),
        Binding("pageup", "page_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page down", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False, priority=True),
    ]

    CSS = """
    MCPViewerScreen {
        align: center middle;
    }

    MCPViewerScreen > Vertical {
        width: 80;
        max-width: 90%;
        height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    MCPViewerScreen .mcp-viewer-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    MCPViewerScreen .mcp-list {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
    }

    MCPViewerScreen .mcp-server-header {
        color: $primary;
        margin-top: 1;
    }

    MCPViewerScreen .mcp-list > .mcp-server-header:first-child {
        margin-top: 0;
    }

    MCPViewerScreen .mcp-tool-item {
        height: 1;
        padding: 0 1;
    }

    MCPViewerScreen .mcp-tool-item:hover {
        background: $surface-lighten-1;
    }

    MCPViewerScreen .mcp-tool-selected {
        /* Deep-marsh highlight + bright neon text — easier on the eyes
         * than the previous full-neon background (which torched white
         * letters with too much contrast). */
        background: #1a4028;
        text-style: bold;
        color: #8effb3;
    }

    MCPViewerScreen .mcp-tool-selected:hover {
        background: #1a4028;
    }

    MCPViewerScreen .mcp-empty {
        color: $text;
        text-align: left;
        margin: 1 2;
    }

    MCPViewerScreen .mcp-viewer-help {
        height: auto;
        color: $text-muted;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, server_info: list[MCPServerInfo]) -> None:
        """Initialize the MCP viewer screen.

        Args:
            server_info: List of MCP server metadata to display.
        """
        super().__init__()
        self._server_info = server_info
        self._tool_widgets: list[MCPToolItem] = []
        self._selected_index = 0

    def compose(self) -> ComposeResult:
        """Compose the screen layout.

        Yields:
            Widgets for the MCP viewer UI.
        """
        glyphs = get_glyphs()
        total_servers = len(self._server_info)
        total_tools = sum(len(s.tools) for s in self._server_info)

        with Vertical():
            if total_servers:
                server_label = "server" if total_servers == 1 else "servers"
                tool_label = "tool" if total_tools == 1 else "tools"
                title = (
                    f"MCP Servers ({total_servers} {server_label},"
                    f" {total_tools} {tool_label})"
                )
            else:
                title = "MCP Servers"
            yield Static(title, classes="mcp-viewer-title")

            with VerticalScroll(classes="mcp-list"):
                if not self._server_info:
                    yield Static(
                        "[bold]No MCP servers active in this session.[/bold]\n"
                        "\n"
                        "[dim]MCP servers extend the agent with external tools — "
                        "Jira, GitHub, Postgres, AWS, Slack, and many more.[/dim]\n"
                        "\n"
                        "[bold]Browse + install (close this viewer first):[/bold]\n"
                        "  [#66ff99]/mcp marketplace[/#66ff99]   browse the full catalog (35+ servers)\n"
                        "  [#66ff99]/mcp featured[/#66ff99]      curated quick-pick list\n"
                        "  [#66ff99]/mcp install <id>[/#66ff99]  install from the catalog\n"
                        "  [#66ff99]/mcp add <name> <cmd>[/#66ff99] add any custom stdio server\n"
                        "  [#66ff99]/mcp list[/#66ff99]          list configured servers\n"
                        "  [#66ff99]/mcp help[/#66ff99]          full reference\n"
                        "\n"
                        "[dim]Once installed, restart the CLI for the agent to pick them up.[/dim]",
                        classes="mcp-empty",
                    )
                else:
                    flat_index = 0
                    for server in self._server_info:
                        tool_count = len(server.tools)
                        t_label = "tool" if tool_count == 1 else "tools"
                        # server.name / server.transport are server-controlled
                        # and untrusted — escape markup + strip control/bidi
                        # before interpolating into Rich markup.
                        yield Static(
                            f"[bold]{_safe_markup(server.name)}[/bold]"
                            f" [dim]{_safe_markup(server.transport)}"
                            f" {glyphs.bullet}"
                            f" {tool_count} {t_label}[/dim]",
                            classes="mcp-server-header",
                        )
                        for tool in server.tools:
                            classes = "mcp-tool-item"
                            if flat_index == 0:
                                classes += " mcp-tool-selected"
                            widget = MCPToolItem(
                                name=tool.name,
                                description=tool.description,
                                index=flat_index,
                                classes=classes,
                            )
                            self._tool_widgets.append(widget)
                            yield widget
                            flat_index += 1

            # Footer always shows controls + how to manage. Distinct
            # from the empty-state body above so users with active
            # servers ALSO see how to add more.
            if self._server_info:
                help_text = (
                    f"[bold]{glyphs.arrow_up}/{glyphs.arrow_down}[/bold] navigate"
                    f"  {glyphs.bullet}  [bold]Enter[/bold] expand/collapse"
                    f"  {glyphs.bullet}  [bold]Esc[/bold] close"
                    f"\n[dim]Manage servers: [#66ff99]/mcp marketplace[/#66ff99] "
                    f"{glyphs.bullet} [#66ff99]/mcp install <id>[/#66ff99] "
                    f"{glyphs.bullet} [#66ff99]/mcp remove <name>[/#66ff99] "
                    f"{glyphs.bullet} [#66ff99]/mcp help[/#66ff99][/dim]"
                )
            else:
                help_text = "[bold]Esc[/bold] close"
            yield Static(help_text, classes="mcp-viewer-help")

    async def on_mount(self) -> None:
        """Apply ASCII border fallback if needed."""
        if _detect_charset_mode() == CharsetMode.ASCII:
            container = self.query_one(Vertical)
            container.styles.border = ("ascii", "green")

    def _move_to(self, index: int) -> None:
        """Move selection to the given index.

        Args:
            index: Target tool index.
        """
        if not self._tool_widgets:
            return
        old = self._selected_index
        self._selected_index = index

        if old != index:
            self._tool_widgets[old].remove_class("mcp-tool-selected")
            self._tool_widgets[index].add_class("mcp-tool-selected")
            self._tool_widgets[index].scroll_visible()

    def _move_selection(self, delta: int) -> None:
        """Move selection by delta positions.

        Args:
            delta: Number of positions to move.
        """
        if not self._tool_widgets:
            return
        count = len(self._tool_widgets)
        target = (self._selected_index + delta) % count
        self._move_to(target)

    def action_move_up(self) -> None:
        """Move selection up."""
        self._move_selection(-1)

    def action_move_down(self) -> None:
        """Move selection down."""
        self._move_selection(1)

    def action_toggle_expand(self) -> None:
        """Toggle expand/collapse on the selected tool."""
        if self._tool_widgets:
            self._tool_widgets[self._selected_index].toggle_expand()

    def action_page_up(self) -> None:
        """Scroll up by one page."""
        scroll = self.query_one(".mcp-list", VerticalScroll)
        scroll.scroll_page_up()

    def action_page_down(self) -> None:
        """Scroll down by one page."""
        scroll = self.query_one(".mcp-list", VerticalScroll)
        scroll.scroll_page_down()

    def action_cancel(self) -> None:
        """Close the viewer."""
        self.dismiss(None)
