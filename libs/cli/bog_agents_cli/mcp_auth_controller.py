"""App-facing glue for the `/mcp login|logout|status` subcommands.

A thin controller between `BogAgentsApp._handle_mcp_command` and
`mcp_login_controller`: it resolves a configured remote server's URL and
transport, drives login inside a Textual worker (so the loopback callback wait
and browser launch never block the event loop), and formats token-safe result
messages. Nothing here surfaces a token value — only structural outcomes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bog_agents_cli.widgets.messages import AppMessage

if TYPE_CHECKING:
    from bog_agents_cli.app import BogAgentsApp

logger = logging.getLogger(__name__)

_REMOTE_TRANSPORTS = frozenset({"http", "sse"})


def _resolve_remote_server(name: str) -> tuple[str, str] | None:
    """Return `(url, transport)` for a configured remote server, or `None`.

    Looks in the user-level config first, then falls back to the merged
    auto-discovered configs (global + project `.mcp.json`) so login works for
    any configured remote server. stdio servers and servers without a usable
    URL return `None`.

    Args:
        name: Configured MCP server name.

    Returns:
        A `(url, transport)` pair for a remote server, or `None`.
    """
    from bog_agents_cli import mcp_config_manager

    cfg: Any = mcp_config_manager.get_server(name)
    if cfg is None:
        from bog_agents_cli.mcp_tools import (
            discover_mcp_configs,
            load_mcp_config_lenient,
            merge_mcp_configs,
        )

        configs = [
            loaded
            for path in discover_mcp_configs()
            if (loaded := load_mcp_config_lenient(path)) is not None
        ]
        cfg = merge_mcp_configs(configs).get("mcpServers", {}).get(name)

    if not isinstance(cfg, dict):
        return None
    transport = cfg.get("type") or cfg.get("transport", "stdio")
    if transport not in _REMOTE_TRANSPORTS:
        return None
    url = cfg.get("url")
    if not isinstance(url, str) or not url:
        return None
    return url, transport


async def _run_login(app: BogAgentsApp, name: str, url: str, transport: str) -> None:
    """Drive one OAuth login and surface a token-safe result message."""
    from bog_agents_cli import mcp_login_controller

    await app._mount_message(
        AppMessage(
            f"Signing in to MCP server [cyan]{name}[/cyan]... "
            "a browser window should open to complete authorization."
        )
    )
    try:
        result = await mcp_login_controller.login(name, url, transport=transport)
    except (
        Exception
    ) as exc:  # worker boundary: never let an exception escape into the UI
        # Log only the sanitized class-name chain, NOT logger.exception: an OAuth
        # exception's repr/traceback can embed a token, and this is the one path
        # that bypasses login()'s own _safe_error sanitizer.
        logger.warning(
            "MCP login worker for %r failed: %s",
            name,
            mcp_login_controller._safe_error(exc),
        )
        await app._mount_message(
            AppMessage(f"[red]Login to '{name}' failed unexpectedly.[/red]")
        )
        return
    colour = "green" if result.ok else "red"
    await app._mount_message(AppMessage(f"[{colour}]{result.message}[/{colour}]"))


async def handle_mcp_auth_command(
    app: BogAgentsApp,
    subcommand: str,
    rest: str,
) -> None:
    """Handle `/mcp login|logout|status`.

    Args:
        app: The running `BogAgentsApp`.
        subcommand: One of `"login"`, `"logout"`, or `"status"`.
        rest: The remainder of the command (the server name for login/logout).
    """
    if subcommand in {"login", "logout"}:
        name = rest.strip()
        if not name:
            await app._mount_message(
                AppMessage(f"Usage: [bold]/mcp {subcommand} <server>[/bold]")
            )
            return

    if subcommand == "login":
        resolved = _resolve_remote_server(name)
        if resolved is None:
            await app._mount_message(
                AppMessage(
                    f"No remote (http/sse) MCP server named [cyan]{name}[/cyan] "
                    "is configured. Use [bold]/mcp list[/bold] to see servers."
                )
            )
            return
        url, transport = resolved
        # A worker keeps the up-to-300s loopback wait and the browser launch
        # off the event loop so the TUI stays responsive during authorization.
        app.run_worker(
            _run_login(app, name, url, transport),
            exclusive=False,
        )
        return

    if subcommand == "logout":
        from bog_agents_cli import mcp_login_controller

        removed = mcp_login_controller.logout(name)
        if removed:
            await app._mount_message(
                AppMessage(f"[green]Logged out of MCP server '{name}'.[/green]")
            )
        else:
            await app._mount_message(
                AppMessage(f"No stored OAuth token for MCP server '{name}'.")
            )
        return

    # status
    await app._mount_message(_render_status())


def _render_status() -> AppMessage:
    """Render OAuth login status for every configured server as a message."""
    import time

    from bog_agents_cli import mcp_config_manager, mcp_login_controller

    servers = mcp_config_manager.list_servers()
    remote = {
        name: cfg
        for name, cfg in servers.items()
        if (cfg.get("type") or cfg.get("transport", "stdio")) in _REMOTE_TRANSPORTS
    }
    if not remote:
        return AppMessage(
            "No remote (http/sse) MCP servers are configured, so there is "
            "nothing to authenticate. Add one with [bold]/mcp add[/bold] or "
            "[bold]/mcp install[/bold]."
        )
    lines = ["[bold]MCP OAuth status[/bold]\n"]
    now = time.time()
    for name in sorted(remote):
        info = mcp_login_controller.status(name)
        if not info["has_token"]:
            state = "[dim]not signed in[/dim]"
        else:
            expires_at = info["expires_at"]
            if isinstance(expires_at, (int, float)) and expires_at <= now:
                state = "[yellow]token expired — will refresh or re-login[/yellow]"
            else:
                state = "[green]signed in[/green]"
        lines.append(f"  [cyan]{name}[/cyan]  {state}")
    lines.append(
        "\n[dim]Sign in with [bold]/mcp login <server>[/bold], "
        "sign out with [bold]/mcp logout <server>[/bold].[/dim]"
    )
    return AppMessage("\n".join(lines))
