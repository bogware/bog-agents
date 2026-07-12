"""Tests for the MCP viewer modal screen."""

from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from bog_agents_cli.mcp_tools import MCPServerInfo, MCPToolInfo
from bog_agents_cli.widgets.mcp_viewer import (
    MCPToolItem,
    MCPViewerScreen,
)


def _widget_text(widget: Widget) -> str:
    """Extract plain text content from a Static widget."""
    content = widget._Static__content  # type: ignore[attr-defined]
    return str(content)


class MCPViewerTestApp(App[None]):
    """Minimal app wrapper for testing MCPViewerScreen."""

    def compose(self) -> ComposeResult:
        yield Static("base")


def _sample_info() -> list[MCPServerInfo]:
    return [
        MCPServerInfo(
            name="filesystem",
            transport="stdio",
            tools=[
                MCPToolInfo(name="read_file", description="Read a file"),
                MCPToolInfo(name="write_file", description="Write a file"),
            ],
        ),
        MCPServerInfo(
            name="remote-api",
            transport="sse",
            tools=[
                MCPToolInfo(name="search", description="Search the web"),
            ],
        ),
    ]


class TestMCPViewerScreen:
    """Tests for the MCP viewer screen widget."""

    async def test_render_with_servers(self) -> None:
        """Viewer displays server names, transports, and tool info."""
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=_sample_info())
            app.push_screen(screen)
            await pilot.pause()

            title = screen.query_one(".mcp-viewer-title", Static)
            assert "2 servers" in _widget_text(title)
            assert "3 tools" in _widget_text(title)

            headers = screen.query(".mcp-server-header")
            assert len(headers) == 2
            assert "filesystem" in _widget_text(headers[0])
            assert "remote-api" in _widget_text(headers[1])

            tools = screen.query(".mcp-tool-item")
            assert len(tools) == 3

    async def test_render_empty_state(self) -> None:
        """Viewer shows the onboarding guide when no servers configured.

        Pre-0.8.0 this asserted the dead-end ``--mcp-config`` hint.
        The empty state was rewritten to walk the user through the
        actual entry points (``/mcp marketplace`` etc.), so the
        assertion now checks for the marketplace pointer instead.
        """
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=[])
            app.push_screen(screen)
            await pilot.pause()

            title = screen.query_one(".mcp-viewer-title", Static)
            assert "MCP Servers" in _widget_text(title)

            empty_text = _widget_text(screen.query_one(".mcp-empty", Static))
            assert "No MCP servers active" in empty_text
            assert "/mcp marketplace" in empty_text

    async def test_escape_dismisses(self) -> None:
        """Pressing Escape closes the viewer."""
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            dismissed = False

            def on_dismiss(result: None) -> None:
                nonlocal dismissed
                dismissed = True

            screen = MCPViewerScreen(server_info=[])
            app.push_screen(screen, on_dismiss)
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            assert dismissed

    async def test_single_server_singular_labels(self) -> None:
        """Title uses singular forms for 1 server and 1 tool."""
        info = [
            MCPServerInfo(
                name="only",
                transport="http",
                tools=[MCPToolInfo(name="do_thing", description="")],
            ),
        ]
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=info)
            app.push_screen(screen)
            await pilot.pause()

            title = screen.query_one(".mcp-viewer-title", Static)
            text = _widget_text(title)
            assert "1 server," in text
            assert "1 tool)" in text

    async def test_keyboard_navigation(self) -> None:
        """Up/down keys move selection between tools."""
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=_sample_info())
            app.push_screen(screen)
            await pilot.pause()

            # First tool starts selected
            assert screen._selected_index == 0
            assert screen._tool_widgets[0].has_class("mcp-tool-selected")

            # Move down
            await pilot.press("down")
            await pilot.pause()
            assert screen._selected_index == 1
            assert screen._tool_widgets[1].has_class("mcp-tool-selected")
            assert not screen._tool_widgets[0].has_class("mcp-tool-selected")

            # Move down again
            await pilot.press("j")
            await pilot.pause()
            assert screen._selected_index == 2

            # Wrap around
            await pilot.press("down")
            await pilot.pause()
            assert screen._selected_index == 0

    async def test_enter_toggles_expand(self) -> None:
        """Enter key expands and collapses tool description."""
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=_sample_info())
            app.push_screen(screen)
            await pilot.pause()

            widget = screen._tool_widgets[0]
            assert isinstance(widget, MCPToolItem)
            assert not widget._expanded

            # Expand
            await pilot.press("enter")
            await pilot.pause()
            assert widget._expanded
            rendered = _widget_text(widget)
            assert "read_file" in rendered
            assert "Read a file" in rendered

            # Collapse
            await pilot.press("enter")
            await pilot.pause()
            assert not widget._expanded

    async def test_click_expands_tool(self) -> None:
        """Clicking a tool selects it and toggles expand."""
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=_sample_info())
            app.push_screen(screen)
            await pilot.pause()

            widget = screen._tool_widgets[0]
            assert not widget._expanded

            await pilot.click(MCPToolItem)
            await pilot.pause()
            assert widget._expanded


# --- SEC-2: markup injection / control-char / trojan-source hardening --------

# Fully server-controlled, hostile MCP metadata. Covers: active Rich markup
# tags, a bare closing tag (crashes unescaped Rich with MarkupError), a raw
# ANSI/CSI escape, an ESC byte glued directly to a markup tag (the case that
# defeats escape-before-sanitize ordering), and a bidi RIGHT-TO-LEFT OVERRIDE.
_HOSTILE_NAME = "read[bold red]x[/bold red]"
_HOSTILE_DESC = "desc[/dim]\x1b[31mRED\x1b[0m\x1b[/dim]tail\u202eevil"
_HOSTILE_SERVER = "srv[bold]inject[/bold]\x1b[/dim]"
_HOSTILE_TRANSPORT = "std[/dim]io\u202e"


def _hostile_info() -> list[MCPServerInfo]:
    return [
        MCPServerInfo(
            name=_HOSTILE_SERVER,
            transport=_HOSTILE_TRANSPORT,
            tools=[MCPToolInfo(name=_HOSTILE_NAME, description=_HOSTILE_DESC)],
        ),
    ]


def _rendered_plain(widget: Static) -> str:
    """Render the Static's stored markup to visible plain text.

    Parses the exact markup string the widget will hand to Rich. Raises
    `MarkupError` if any active tag survived sanitization — precisely the crash
    SEC-2 must prevent.
    """
    from rich.markup import render as render_markup

    return render_markup(_widget_text(widget)).plain


class TestMCPViewerMarkupSafety:
    """SEC-2: untrusted server-controlled text must never crash or inject."""

    async def test_hostile_metadata_mounts_and_renders_literally(self) -> None:
        """Hostile tool/server metadata mounts without MarkupError.

        The dangerous tokens must render as literal text (escaped), terminal
        escape sequences and bidi overrides must be gone, and no active markup
        may survive to raise at render time.
        """
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=_hostile_info())
            # Mounting composes every untrusted Static; a MarkupError here
            # would surface as a failed push_screen.
            app.push_screen(screen)
            await pilot.pause()

            header = screen.query_one(".mcp-server-header", Static)
            tool = screen.query_one(".mcp-tool-item", Static)

            # Rendering exercises Rich's markup parser end-to-end. If any active
            # tag survived sanitization this raises MarkupError and fails.
            header_plain = _rendered_plain(header)
            tool_plain = _rendered_plain(tool)

            # Literal tag text is preserved verbatim (escaped, not interpreted).
            assert "[bold red]x[/bold red]" in tool_plain
            assert "[bold]inject[/bold]" in header_plain

            # No raw ESC byte or bidi override leaks into rendered output.
            for plain in (header_plain, tool_plain):
                assert "\x1b" not in plain
                assert "\u202e" not in plain

    async def test_hostile_expanded_view_is_safe(self) -> None:
        """Expanding a hostile tool renders full description without crashing."""
        app = MCPViewerTestApp()
        async with app.run_test() as pilot:
            screen = MCPViewerScreen(server_info=_hostile_info())
            app.push_screen(screen)
            await pilot.pause()

            widget = screen._tool_widgets[0]
            await pilot.press("enter")
            await pilot.pause()
            assert widget._expanded

            plain = _rendered_plain(widget)
            # Description tag text shown literally; escape/bidi stripped.
            assert "[/dim]" in plain
            assert "\x1b" not in plain
            assert "\u202e" not in plain

    async def test_stored_metadata_is_sanitized(self) -> None:
        """MCPToolItem stores the sanitized (escaped) name/description."""
        item = MCPToolItem(
            name=_HOSTILE_NAME,
            description=_HOSTILE_DESC,
            index=0,
        )
        # Escaped markup is backslash-prefixed; raw escape/bidi bytes are gone.
        assert "\\[bold red]" in item.tool_name
        assert "\x1b" not in item.tool_description
        assert "\u202e" not in item.tool_description
        # Idempotent: no active-tag / control-byte residue remains.
        from rich.markup import render

        render(item.tool_name)
        render(item.tool_description)
