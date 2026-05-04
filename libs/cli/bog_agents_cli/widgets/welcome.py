"""Welcome banner widget for bog-agents-cli."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.events import Click

from bog_agents_cli.config import (
    COLORS,
    _is_editable_install,
    fetch_langsmith_project_url,
    get_banner,
    get_glyphs,
    get_langsmith_project_name,
    newline_shortcut,
)
from bog_agents_cli.widgets._links import open_style_link


class WelcomeBanner(Static):
    """Welcome banner displayed at startup."""

    # Disable Textual's auto_links to prevent a flicker cycle: Style.__add__
    # calls .copy() for linked styles, generating a fresh random _link_id on
    # each render. This means highlight_link_id never stabilizes, causing an
    # infinite hover-refresh loop.
    auto_links = False

    DEFAULT_CSS = """
    WelcomeBanner {
        height: auto;
        padding: 1 2;
        margin-bottom: 1;
        background: $surface-darken-1;
        border: round $primary;
    }
    """

    def __init__(
        self,
        thread_id: str | None = None,
        mcp_tool_count: int = 0,
        *,
        connecting: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the welcome banner.

        Args:
            thread_id: Optional thread ID to display in the banner.
            mcp_tool_count: Number of MCP tools loaded at startup.
            connecting: When `True`, show a "Connecting..." footer instead of
                the normal ready prompt. Call `set_connected` to transition.
            **kwargs: Additional arguments passed to parent.
        """
        # Avoid collision with Widget._thread_id (Textual internal int)
        self._cli_thread_id: str | None = thread_id
        self._mcp_tool_count = mcp_tool_count
        self._connecting = connecting
        self._failed = False
        self._failure_error: str = ""
        self._project_name: str | None = get_langsmith_project_name()
        self._project_url: str | None = None

        super().__init__(self._build_banner(), **kwargs)

    def on_mount(self) -> None:
        """Kick off background fetch for LangSmith project URL."""
        if self._project_name:
            self.run_worker(self._fetch_and_update, exclusive=True)

    async def _fetch_and_update(self) -> None:
        """Fetch the LangSmith URL in a thread and update the banner."""
        if not self._project_name:
            return
        try:
            project_url = await asyncio.wait_for(
                asyncio.to_thread(fetch_langsmith_project_url, self._project_name),
                timeout=2.0,
            )
        except (TimeoutError, OSError):
            project_url = None
        if project_url:
            self._project_url = project_url
            self.update(self._build_banner(project_url))

    def update_thread_id(self, thread_id: str) -> None:
        """Update the displayed thread ID and re-render the banner.

        Args:
            thread_id: The new thread ID to display.
        """
        self._cli_thread_id = thread_id
        self.update(self._build_banner(self._project_url))

    def set_connected(self, mcp_tool_count: int = 0) -> None:
        """Transition from "connecting" to "ready" state.

        Args:
            mcp_tool_count: Number of MCP tools loaded during connection.
        """
        self._connecting = False
        self._failed = False
        self._mcp_tool_count = mcp_tool_count
        self.update(self._build_banner(self._project_url))

    def set_failed(self, error: str) -> None:
        """Transition from "connecting" to a persistent failure state.

        Args:
            error: Error message describing the server startup failure.
        """
        self._connecting = False
        self._failed = True
        self._failure_error = error
        self.update(self._build_banner(self._project_url))

    def on_click(self, event: Click) -> None:  # noqa: PLR6301  # Textual event handler
        """Open Rich-style hyperlinks on single click."""
        open_style_link(event)

    def _build_banner(self, project_url: str | None = None) -> Text:
        """Build the banner rich text.

        When a `project_url` is provided and a thread ID is set, the thread ID
        is rendered as a clickable hyperlink to the LangSmith thread view.

        Args:
            project_url: LangSmith project URL used for linking the project
                name and thread ID. When `None`, text is rendered without links.

        Returns:
            Rich Text object containing the formatted banner.
        """
        banner = Text()
        # Use orange for local, green for production
        banner_color = (
            COLORS["primary_dev"] if _is_editable_install() else COLORS["primary"]
        )
        raw_banner = get_banner()
        banner.append(raw_banner + "\n", style=Style(bold=True, color=banner_color))
        banner.append(
            "Terminal engineering cockpit for code, context, and execution.\n",
            style=Style(color=COLORS["dim"], italic=True),
        )

        if self._project_name:
            banner.append(
                "LangSmith",
                style=Style(
                    bold=True,
                    color="#050a07",
                    bgcolor=COLORS["thinking"],
                ),
            )
            banner.append(" tracing: ", style=Style(color=COLORS["dim"]))
            if project_url:
                banner.append(
                    f"'{self._project_name}'",
                    style=Style(
                        color="cyan",
                        link=f"{project_url}?utm_source=bog-agents-cli",
                    ),
                )
            else:
                banner.append(f"'{self._project_name}'", style="cyan")
            banner.append("\n")

        if self._cli_thread_id:
            if project_url:
                thread_url = (
                    f"{project_url.rstrip('/')}/t/{self._cli_thread_id}"
                    "?utm_source=bog-agents-cli"
                )
                thread_line = Text.assemble(
                    (
                        "Thread",
                        Style(
                            bold=True,
                            color="#050a07",
                            bgcolor=COLORS["primary"],
                        ),
                    ),
                    (": ", "dim"),
                    (self._cli_thread_id, Style(dim=True, link=thread_url)),
                    ("\n", "dim"),
                )
                banner.append_text(thread_line)
            else:
                banner.append(
                    "Thread",
                    style=Style(
                        bold=True,
                        color="#050a07",
                        bgcolor=COLORS["primary"],
                    ),
                )
                banner.append(f": {self._cli_thread_id}\n", style="dim")

        if self._mcp_tool_count > 0:
            banner.append(
                "MCP",
                style=Style(
                    bold=True,
                    color="#050a07",
                    bgcolor=COLORS["tool"],
                ),
            )
            banner.append(" ready: ", style=Style(color=COLORS["dim"]))
            label = "MCP tool" if self._mcp_tool_count == 1 else "MCP tools"
            banner.append(f"Loaded {self._mcp_tool_count} {label}\n")

        if self._failed:
            banner.append_text(build_failure_footer(self._failure_error))
        elif self._connecting:
            banner.append_text(build_connecting_footer())
        else:
            banner.append_text(build_welcome_footer())
        return banner


def build_failure_footer(error: str) -> Text:
    """Build a footer shown when the server failed to start.

    Args:
        error: Error message describing the failure.

    Returns:
        Rich Text with a persistent failure message.
    """
    footer = Text()
    footer.append("\nServer failed to start: ", style="bold red")
    footer.append(error, style="red")
    footer.append("\n", style="red")
    return footer


def build_connecting_footer() -> Text:
    """Build a footer shown while waiting for the server to connect.

    Returns:
        Rich Text with a connecting status message.
    """
    footer = Text()
    footer.append("\nConnecting to server...\n", style="dim")
    return footer


def build_welcome_footer() -> Text:
    """Build the two-line footer shown at the bottom of the welcome banner.

    Returns:
        Rich Text with the ready prompt and keyboard shortcut help line.
    """
    footer = Text()
    footer.append(
        "\nReady to code! What would you like to build?\n",
        style=Style(color=COLORS["user"], bold=True),
    )
    bullet = get_glyphs().bullet
    footer.append("Enter send", style=Style(color=COLORS["primary"], bold=True))
    footer.append(f" {bullet} ", style=Style(color=COLORS["dim"]))
    footer.append(newline_shortcut(), style=Style(color=COLORS["thinking"]))
    footer.append(" newline", style=Style(color=COLORS["dim"]))
    footer.append(f" {bullet} ", style=Style(color=COLORS["dim"]))
    footer.append("@ files", style=Style(color=COLORS["thinking"]))
    footer.append(f" {bullet} ", style=Style(color=COLORS["dim"]))
    footer.append("/ commands", style=Style(color=COLORS["tool"]))
    return footer
