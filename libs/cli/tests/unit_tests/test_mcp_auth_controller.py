"""Tests for `bog_agents_cli.mcp_auth_controller` and the /mcp OAuth surface.

Exercises the thin app-glue for `/mcp login|logout|status` without a TUI: a
fake app captures mounted messages and launched workers. Also asserts the
login/logout/status subcommands are registered on the `/mcp` spec.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents_cli import mcp_auth_controller
from bog_agents_cli.mcp_auth_controller import (
    _render_status,
    _resolve_remote_server,
    handle_mcp_auth_command,
)


@pytest.fixture(autouse=True)
def _allow_real_sockets():
    """Permit real sockets: login delegation can build a loopback server
    (127.0.0.1 inet bind), which CI's Linux `--disable-socket` blocks. No-op
    under Windows CI's `-p no:socket`.
    """
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    yield


class _FakeApp:
    """Minimal stand-in for BogAgentsApp used by the controller."""

    def __init__(self) -> None:
        self.messages: list[object] = []
        self.workers: list[object] = []

    async def _mount_message(self, message: object) -> None:
        self.messages.append(message)

    def run_worker(self, coro: object, *, exclusive: bool = False) -> None:
        del exclusive
        self.workers.append(coro)
        # Avoid an un-awaited-coroutine warning without running the network path.
        coro.close()  # type: ignore[attr-defined]

    @property
    def texts(self) -> list[str]:
        return [getattr(m, "_content", "") for m in self.messages]


# ---- subcommand registration ------------------------------------------------


def test_mcp_login_logout_status_registered() -> None:
    """The /mcp spec advertises login, logout, and status subcommands."""
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS

    mcp = next(s for s in SLASH_COMMAND_SPECS if s.name == "/mcp")
    names = {name for name, _desc in mcp.subcommands}
    assert {"login", "logout", "status"} <= names


def test_mcp_handler_still_registered() -> None:
    """/mcp continues to route to its single handler method."""
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert COMMAND_HANDLER_MAP["/mcp"] == "_handle_mcp_command"


# ---- _resolve_remote_server -------------------------------------------------


def test_resolve_remote_server_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured http server resolves to (url, transport)."""
    monkeypatch.setattr(
        "bog_agents_cli.mcp_config_manager.get_server",
        lambda name: (
            {"type": "http", "url": "https://x/mcp"} if name == "srv" else None
        ),
    )
    assert _resolve_remote_server("srv") == ("https://x/mcp", "http")


def test_resolve_remote_server_stdio_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stdio server cannot be logged in to — resolves to None."""
    monkeypatch.setattr(
        "bog_agents_cli.mcp_config_manager.get_server",
        lambda name: {"command": "npx"},
    )
    assert _resolve_remote_server("srv") is None


def test_resolve_remote_server_unknown_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown server (absent from user + discovered configs) is None."""
    monkeypatch.setattr(
        "bog_agents_cli.mcp_config_manager.get_server", lambda name: None
    )
    monkeypatch.setattr(
        "bog_agents_cli.mcp_tools.discover_mcp_configs", lambda **_kw: []
    )
    assert _resolve_remote_server("nope") is None


# ---- handle_mcp_auth_command ------------------------------------------------


async def test_login_requires_server_name() -> None:
    """`/mcp login` with no server name shows a usage message, no worker."""
    app = _FakeApp()
    await handle_mcp_auth_command(app, "login", "")
    assert app.workers == []
    assert any("Usage" in t for t in app.texts)


async def test_login_unknown_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp login <unknown>` reports no such remote server, no worker."""
    monkeypatch.setattr(mcp_auth_controller, "_resolve_remote_server", lambda n: None)
    app = _FakeApp()
    await handle_mcp_auth_command(app, "login", "ghost")
    assert app.workers == []
    assert any("ghost" in t for t in app.texts)


async def test_login_launches_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp login <server>` launches exactly one background worker."""
    monkeypatch.setattr(
        mcp_auth_controller,
        "_resolve_remote_server",
        lambda n: ("https://x/mcp", "http"),
    )
    app = _FakeApp()
    await handle_mcp_auth_command(app, "login", "srv")
    assert len(app.workers) == 1


async def test_logout_removes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp logout <server>` reports success when a token was removed."""
    monkeypatch.setattr("bog_agents_cli.mcp_login_controller.logout", lambda name: True)
    app = _FakeApp()
    await handle_mcp_auth_command(app, "logout", "srv")
    assert any("Logged out" in t for t in app.texts)


async def test_logout_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp logout <server>` reports when there was nothing to remove."""
    monkeypatch.setattr(
        "bog_agents_cli.mcp_login_controller.logout", lambda name: False
    )
    app = _FakeApp()
    await handle_mcp_auth_command(app, "logout", "srv")
    assert any("No stored OAuth token" in t for t in app.texts)


async def test_status_lists_remote_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp status` renders one line per remote server, skipping stdio."""
    monkeypatch.setattr(
        "bog_agents_cli.mcp_config_manager.list_servers",
        lambda: {
            "remote": {"type": "http", "url": "https://x/mcp"},
            "local": {"command": "npx"},
        },
    )
    monkeypatch.setattr(
        "bog_agents_cli.mcp_login_controller.status",
        lambda name: {"has_token": False, "expires_at": None},
    )
    msg = _render_status()
    text = getattr(msg, "_content", "")
    assert "remote" in text
    assert "local" not in text


async def test_status_no_remote_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/mcp status` with only stdio servers explains there is nothing to auth."""
    monkeypatch.setattr(
        "bog_agents_cli.mcp_config_manager.list_servers",
        lambda: {"local": {"command": "npx"}},
    )
    msg = _render_status()
    assert "nothing to authenticate" in getattr(msg, "_content", "")
