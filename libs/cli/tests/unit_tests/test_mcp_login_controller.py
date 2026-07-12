"""Tests for `bog_agents_cli.mcp_login_controller`.

`login` is driven with a fake handshake + fake provider so no real browser or
network is involved: the fake handshake persists a token through the injected
storage, mirroring what the MCP SDK would do on a successful exchange. Also
covers logout, status, failure surfacing, and the no-token-leak invariant.
"""

from __future__ import annotations

import time

import pytest
from mcp.shared.auth import OAuthToken

from bog_agents_cli import mcp_login_controller, mcp_token_storage
from bog_agents_cli.mcp_login_controller import login, logout, status
from bog_agents_cli.mcp_loopback import LoopbackCallbackServer, LoopbackTimeoutError
from bog_agents_cli.mcp_token_storage import FileTokenStorage

SECRET = "super-secret-access-token"  # test fixture, not a real credential


@pytest.fixture(autouse=True)
def _redirect_storage(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point all default token storage at a temp dir for the whole module."""
    monkeypatch.setattr(
        mcp_token_storage,
        "default_oauth_dir",
        lambda: tmp_path,
    )
    return tmp_path


def _fake_loopback_factory() -> LoopbackCallbackServer:
    # A real ephemeral loopback is cheap and closed in login()'s finally.
    return LoopbackCallbackServer()


async def test_login_persists_token_and_returns_ok() -> None:
    """A successful handshake stores a token and yields ok without the token."""
    captured: dict[str, object] = {}

    def fake_provider_factory(server_name, server_url, *, storage, **_kw):
        captured["storage"] = storage
        return object()

    async def fake_handshake(*, provider, server_name, server_url, transport):
        # Emulate the SDK persisting a token after the exchange.
        await captured["storage"].set_tokens(  # ty: ignore[possibly-unbound-attribute]
            OAuthToken(access_token=SECRET, token_type="Bearer", expires_in=3600),
        )

    result = await login(
        "srv",
        "https://mcp.example.com/mcp",
        loopback_factory=_fake_loopback_factory,
        provider_factory=fake_provider_factory,
        handshake=fake_handshake,
    )
    assert result.ok is True
    assert result.server_name == "srv"
    assert result.expires_at is not None
    assert result.expires_at > time.time()
    # The token value must not leak into the user-facing message.
    assert SECRET not in result.message

    # And it is genuinely persisted.
    stored = await FileTokenStorage("srv").get_tokens()
    assert stored is not None
    assert stored.access_token == SECRET


async def test_login_reports_when_no_token_persisted() -> None:
    """A handshake that stores nothing yields a not-ok result."""

    def fake_provider_factory(*_a, **_kw):
        return object()

    async def fake_handshake(**_kw):
        return None

    result = await login(
        "srv",
        "https://mcp.example.com/mcp",
        loopback_factory=_fake_loopback_factory,
        provider_factory=fake_provider_factory,
        handshake=fake_handshake,
    )
    assert result.ok is False
    assert "without persisting a token" in result.message


async def test_login_surfaces_timeout_safely() -> None:
    """A loopback timeout during handshake yields a token-safe failure."""

    def fake_provider_factory(*_a, **_kw):
        return object()

    async def fake_handshake(**_kw):
        msg = "Browser callback was not received before the timeout."
        raise LoopbackTimeoutError(msg)

    result = await login(
        "srv",
        "https://mcp.example.com/mcp",
        loopback_factory=_fake_loopback_factory,
        provider_factory=fake_provider_factory,
        handshake=fake_handshake,
    )
    assert result.ok is False
    assert "timeout" in result.message.lower()


async def test_login_does_not_leak_token_from_exception() -> None:
    """A raised exception carrying token-shaped data never reaches the message."""

    def fake_provider_factory(*_a, **_kw):
        return object()

    async def fake_handshake(**_kw):
        # An opaque SDK-style error whose str() embeds a token.
        raise RuntimeError(f"exchange failed token={SECRET}")

    result = await login(
        "srv",
        "https://mcp.example.com/mcp",
        loopback_factory=_fake_loopback_factory,
        provider_factory=fake_provider_factory,
        handshake=fake_handshake,
    )
    assert result.ok is False
    assert SECRET not in result.message
    assert "RuntimeError" in result.message


async def test_login_rejects_unsafe_server_name() -> None:
    """An unsafe server name fails before any loopback/handshake work."""
    called = False

    def factory() -> LoopbackCallbackServer:
        nonlocal called
        called = True
        return LoopbackCallbackServer()

    result = await login(
        "../evil",
        "https://mcp.example.com/mcp",
        loopback_factory=factory,
    )
    assert result.ok is False
    assert "Invalid MCP server name" in result.message
    assert called is False


async def test_logout_removes_token() -> None:
    """`logout` deletes a stored token file and reports removal."""
    await FileTokenStorage("srv").set_tokens(
        OAuthToken(access_token=SECRET, token_type="Bearer", expires_in=3600),
    )
    assert logout("srv") is True
    assert logout("srv") is False  # nothing left to remove
    assert logout("../evil") is False  # unsafe name, no-op


async def test_status_reports_token_and_expiry() -> None:
    """`status` reports has-token and expiry without exposing the secret."""
    assert status("srv") == {
        "server_name": "srv",
        "has_token": False,
        "expires_at": None,
        "expired": None,
    }

    await FileTokenStorage("srv").set_tokens(
        OAuthToken(access_token=SECRET, token_type="Bearer", expires_in=3600),
    )
    st = status("srv")
    assert st["has_token"] is True
    assert st["expired"] is False
    assert isinstance(st["expires_at"], float)
    # Sanity: the status dict never contains the token value anywhere.
    assert SECRET not in repr(st)


async def test_status_reports_expired_token() -> None:
    """A token already past its expiry is flagged expired."""
    await FileTokenStorage("srv").set_tokens(
        OAuthToken(access_token=SECRET, token_type="Bearer", expires_in=-10),
    )
    st = status("srv")
    assert st["has_token"] is True
    assert st["expired"] is True


def test_status_unsafe_name() -> None:
    """`status` on an unsafe name reports no token without raising."""
    st = status("../evil")
    assert st["has_token"] is False
    assert st["expired"] is None
