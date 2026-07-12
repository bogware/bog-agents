"""Tests for `bog_agents_cli.mcp_oauth` — metadata, provider, and handlers.

No real browser or network: `webbrowser.open` is injected, and the provider is
built against a fake loopback so the redirect/callback handlers can be driven
directly.
"""

from __future__ import annotations

import pytest
from mcp.client.auth import OAuthClientProvider

from bog_agents_cli import mcp_oauth
from bog_agents_cli.mcp_loopback import LoopbackCallbackServer, LoopbackTimeoutError
from bog_agents_cli.mcp_oauth import (
    build_oauth_provider,
    default_client_metadata,
    has_stored_tokens,
)
from bog_agents_cli.mcp_token_storage import FileTokenStorage


@pytest.fixture(autouse=True)
def _allow_real_sockets():
    """Permit real sockets: some tests build a loopback server (127.0.0.1 inet
    bind), which CI's Linux `--disable-socket` blocks. No-op under Windows CI's
    `-p no:socket` (nothing is blocking there).
    """
    try:
        import pytest_socket
    except ImportError:
        yield
        return
    pytest_socket.enable_socket()
    yield


def test_default_client_metadata() -> None:
    """Metadata advertises a public PKCE client named bog-agents."""
    meta = default_client_metadata("http://localhost:5/callback", scope="a b")
    assert meta.client_name == "bog-agents"
    assert [str(u) for u in meta.redirect_uris] == ["http://localhost:5/callback"]
    assert meta.grant_types == ["authorization_code", "refresh_token"]
    assert meta.response_types == ["code"]
    assert meta.token_endpoint_auth_method == "none"
    assert meta.scope == "a b"


def test_build_oauth_provider_returns_provider(tmp_path) -> None:
    """The factory returns a real `OAuthClientProvider` (an httpx.Auth)."""
    storage = FileTokenStorage("srv", base_dir=tmp_path)
    loopback = LoopbackCallbackServer()
    try:
        provider = build_oauth_provider(
            "srv",
            "https://mcp.example.com/mcp",
            storage=storage,
            loopback=loopback,
            open_browser=lambda _url: True,
        )
        assert isinstance(provider, OAuthClientProvider)
    finally:
        loopback.close()


async def test_redirect_handler_opens_browser() -> None:
    """The redirect handler invokes the injected browser opener with the URL."""
    loopback = LoopbackCallbackServer()
    try:
        opened: list[str] = []

        def opener(url: str) -> bool:
            opened.append(url)
            return True

        handler = mcp_oauth._make_redirect_handler(loopback, open_browser=opener)
        await handler("https://auth.example.com/authorize?x=1")
        assert opened == ["https://auth.example.com/authorize?x=1"]
    finally:
        loopback.close()


async def test_redirect_handler_survives_open_failure() -> None:
    """A browser that fails to open does not raise from the redirect handler."""
    loopback = LoopbackCallbackServer()
    try:
        handler = mcp_oauth._make_redirect_handler(
            loopback,
            open_browser=lambda _url: False,
        )
        await handler("https://auth.example.com/authorize")  # must not raise
    finally:
        loopback.close()


async def test_callback_handler_returns_loopback_code() -> None:
    """The callback handler yields the loopback's captured (code, state)."""
    import urllib.request

    loopback = LoopbackCallbackServer()
    try:
        handler = mcp_oauth._make_callback_handler(loopback, timeout=5)
        callback_url = loopback.redirect_uri.replace(
            "http://localhost:", "http://127.0.0.1:"
        )
        with urllib.request.urlopen(  # noqa: ASYNC210  # deliberate blocking GET to trigger the callback before awaiting
            f"{callback_url}?code=CB&state=ST",
            timeout=5,
        ):
            pass
        code, state = await handler()
        assert code == "CB"
        assert state == "ST"
    finally:
        loopback.close()


async def test_callback_handler_paste_back_fallback() -> None:
    """On loopback timeout, the handler falls back to paste-back parsing."""
    loopback = LoopbackCallbackServer()
    try:
        handler = mcp_oauth._make_callback_handler(
            loopback,
            paste_back=lambda: "http://localhost:9/callback?code=PB&state=PS",
            timeout=0.2,
        )
        code, state = await handler()
        assert code == "PB"
        assert state == "PS"
    finally:
        loopback.close()


async def test_callback_handler_timeout_without_paste_back() -> None:
    """Without paste-back, a loopback timeout propagates."""
    loopback = LoopbackCallbackServer()
    try:
        handler = mcp_oauth._make_callback_handler(loopback, timeout=0.2)
        with pytest.raises(LoopbackTimeoutError):
            await handler()
    finally:
        loopback.close()


def test_has_stored_tokens(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`has_stored_tokens` reflects file presence and fail-closes on bad names."""
    monkeypatch.setattr(mcp_oauth, "default_oauth_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "bog_agents_cli.mcp_token_storage.default_oauth_dir",
        lambda: tmp_path,
    )
    assert has_stored_tokens("srv") is False
    (tmp_path / "srv.json").write_text("{}", encoding="utf-8")
    assert has_stored_tokens("srv") is True
    # An unsafe name never raises; it reports False.
    assert has_stored_tokens("../evil") is False


def test_non_interactive_provider_binds_no_loopback(monkeypatch, tmp_path):
    """The refresh-only (connection-path) provider must not bind a loopback socket.

    Regression: `_resolve_mcp_auth` attaches a provider for every stored-token
    server on each tool load. If that eagerly bound a loopback socket + daemon
    thread (as the interactive path does), each reload would leak one. The
    non-interactive provider must never construct a LoopbackCallbackServer.
    """
    constructed = {"count": 0}

    class _Spy(mcp_oauth.LoopbackCallbackServer):
        def __init__(self, *args, **kwargs) -> None:
            constructed["count"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mcp_oauth, "LoopbackCallbackServer", _Spy)

    provider = mcp_oauth.build_oauth_provider(
        "srv",
        "https://example.com/mcp",
        storage=mcp_oauth.FileTokenStorage("srv"),
        interactive=False,
    )

    assert constructed["count"] == 0
    # Still a real, usable httpx.Auth provider.
    assert hasattr(provider, "async_auth_flow")


async def test_non_interactive_handlers_raise_reauth_required():
    """Non-interactive redirect/callback handlers must refuse instead of opening a browser."""
    redirect = mcp_oauth._make_reauth_required_redirect("srv")
    callback = mcp_oauth._make_reauth_required_callback("srv")

    with pytest.raises(mcp_oauth.ReauthRequiredError):
        await redirect("https://auth.example.com/authorize")
    with pytest.raises(mcp_oauth.ReauthRequiredError):
        await callback()
