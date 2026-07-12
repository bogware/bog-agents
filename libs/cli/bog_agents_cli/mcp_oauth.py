"""Spec-compliant OAuth wiring for remote MCP servers.

Builds an `mcp.client.auth.OAuthClientProvider` — an `httpx.Auth` — that the
MCP transport attaches to a remote connection. The provider performs all of
RFC 9728 protected-resource-metadata discovery, Dynamic Client Registration,
PKCE, the authorization-code exchange, and automatic token refresh internally.
This module supplies only the three things the SDK cannot infer:

- storage: a `FileTokenStorage` bound to the server (persists client info +
    tokens under `~/.bog-agents/mcp-oauth/`),
- a redirect handler that opens the user's browser to the authorization URL,
- a callback handler that waits on a loopback server for `(code, state)`.

`has_stored_tokens` is a cheap synchronous check the connection wiring uses to
decide whether to attach a provider at all.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING

from mcp.shared.auth import OAuthClientMetadata

from bog_agents_cli.mcp_loopback import LoopbackCallbackServer, parse_callback_url
from bog_agents_cli.mcp_token_storage import FileTokenStorage

if TYPE_CHECKING:
    from mcp.client.auth import OAuthClientProvider, TokenStorage

logger = logging.getLogger(__name__)

CLIENT_NAME = "bog-agents"
"""`client_name` advertised to the authorization server during DCR."""

# Placeholder redirect URI used for the non-interactive (refresh-only) provider.
# It is never contacted: a non-interactive provider never authorizes, so no
# loopback is bound. A `127.0.0.1` literal (not `localhost`) avoids the IPv6
# resolution pitfall per RFC 8252 s7.3 should it ever be surfaced.
_NONINTERACTIVE_REDIRECT_URI = "http://127.0.0.1/callback"


class ReauthRequiredError(RuntimeError):
    """Raised when a non-interactive provider would need interactive re-login.

    A provider attached at connection time (refresh-only) never opens a browser.
    If its stored token is missing or cannot be refreshed, the SDK reaches for
    the redirect/callback handlers; raising this instead surfaces the actionable
    `/mcp login` path rather than silently launching a browser at startup.
    """


BrowserOpener = Callable[[str], bool]
"""Signature of `webbrowser.open`: takes a URL, returns whether it launched."""


def default_client_metadata(
    redirect_uri: str,
    *,
    scope: str | None = None,
) -> OAuthClientMetadata:
    """Build the client metadata used for Dynamic Client Registration.

    Args:
        redirect_uri: The loopback redirect URI the callback server serves.
        scope: Optional space-delimited scope string to request. `None` lets
            the authorization server apply its default scope.

    Returns:
        An `OAuthClientMetadata` describing a public PKCE client named
            `bog-agents`.
    """
    return OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[redirect_uri],  # ty: ignore[invalid-argument-type]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",  # noqa: S106  # public PKCE client: literally no client secret
        scope=scope,
    )


def _make_redirect_handler(
    loopback: LoopbackCallbackServer,
    *,
    open_browser: BrowserOpener,
) -> Callable[[str], object]:
    """Return an async redirect handler that opens the browser to `auth_url`.

    The MCP SDK awaits this before it awaits the callback handler. Opening the
    browser runs in a worker thread so it never blocks the event loop. A
    browser that fails to launch is not fatal here: the callback handler falls
    back to paste-back, so the login can still complete.
    """

    async def redirect(auth_url: str) -> None:
        try:
            opened = await asyncio.to_thread(open_browser, auth_url)
        except webbrowser.Error:
            opened = False
        if not opened:
            logger.info(
                "Could not open a browser for MCP OAuth on loopback port %d; "
                "paste the redirected callback URL into the terminal to "
                "continue.",
                loopback.port,
            )

    return redirect


def _make_callback_handler(
    loopback: LoopbackCallbackServer,
    *,
    paste_back: Callable[[], str] | None = None,
    timeout: float = 300.0,
) -> Callable[[], object]:
    """Return an async callback handler that waits on the loopback server.

    On a loopback timeout the handler falls back to `paste_back` (when
    supplied) so a headless or firewalled environment can finish by pasting the
    redirect URL. Without a paste-back source, the timeout propagates.
    """

    async def callback() -> tuple[str, str | None]:
        from bog_agents_cli.mcp_loopback import LoopbackTimeoutError

        try:
            return await loopback.wait_for_code(timeout)
        except LoopbackTimeoutError:
            if paste_back is None:
                raise
            url = await asyncio.to_thread(paste_back)
            return parse_callback_url(url)

    return callback


def build_oauth_provider(
    server_name: str,
    server_url: str,
    *,
    storage: TokenStorage | None = None,
    loopback: LoopbackCallbackServer | None = None,
    open_browser: BrowserOpener | None = None,
    scope: str | None = None,
    paste_back: Callable[[], str] | None = None,
    timeout: float = 300.0,
    interactive: bool = True,
) -> OAuthClientProvider:
    """Construct an `OAuthClientProvider` for a remote MCP server.

    Args:
        server_name: Configured MCP server name (used for token storage).
        server_url: Remote MCP server URL the provider authenticates against.
        storage: Token storage to use. Defaults to a `FileTokenStorage` bound
            to `server_name`.
        loopback: A started loopback callback server. Defaults to a fresh
            ephemeral-port `LoopbackCallbackServer`; the caller owns closing it.
            Ignored when `interactive=False`.
        open_browser: Browser-opening callable. Defaults to `webbrowser.open`.
            Injectable so tests avoid launching a real browser.
        scope: Optional space-delimited scope string to request.
        paste_back: Optional callable returning a pasted redirect URL, used as
            a headless fallback when the loopback callback times out.
        timeout: Seconds to wait for the OAuth callback.
        interactive: When `True` (default, the `/mcp login` path), a loopback
            callback server is bound and the browser is opened to authorize.
            When `False` (the connection/refresh path), NO loopback socket is
            bound and NO browser is ever opened: the provider only refreshes an
            existing token, and if it cannot, its handlers raise
            `ReauthRequiredError` so the caller surfaces the `/mcp login` hint.

    Returns:
        A configured `OAuthClientProvider` (an `httpx.Auth`).
    """
    from mcp.client.auth import OAuthClientProvider

    token_storage = storage if storage is not None else FileTokenStorage(server_name)

    if not interactive:
        # Refresh-only: never bind a loopback or open a browser. A stored client
        # registration means DCR is skipped; the placeholder redirect URI is
        # only carried in metadata and never contacted.
        metadata = default_client_metadata(_NONINTERACTIVE_REDIRECT_URI, scope=scope)
        redirect = _make_reauth_required_redirect(server_name)
        callback = _make_reauth_required_callback(server_name)
        return OAuthClientProvider(
            server_url=server_url,
            client_metadata=metadata,
            storage=token_storage,
            redirect_handler=redirect,  # ty: ignore[invalid-argument-type]
            callback_handler=callback,  # ty: ignore[invalid-argument-type]
            timeout=timeout,
        )

    callback_server = loopback if loopback is not None else LoopbackCallbackServer()
    opener = open_browser if open_browser is not None else webbrowser.open

    metadata = default_client_metadata(callback_server.redirect_uri, scope=scope)
    redirect = _make_redirect_handler(callback_server, open_browser=opener)
    callback = _make_callback_handler(
        callback_server,
        paste_back=paste_back,
        timeout=timeout,
    )
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=token_storage,
        redirect_handler=redirect,  # ty: ignore[invalid-argument-type]
        callback_handler=callback,  # ty: ignore[invalid-argument-type]
        timeout=timeout,
    )


def _make_reauth_required_redirect(server_name: str) -> Callable[[str], object]:
    """Return a redirect handler that refuses to open a browser."""

    async def redirect(_auth_url: str) -> None:  # noqa: RUF029 — must be async to satisfy the SDK handler protocol
        raise ReauthRequiredError(
            f"MCP server {server_name!r} requires interactive login; run `/mcp login {server_name}`"
        )

    return redirect


def _make_reauth_required_callback(server_name: str) -> Callable[[], object]:
    """Return a callback handler that signals interactive re-login is required."""

    async def callback() -> tuple[str, str | None]:  # noqa: RUF029 — must be async to satisfy the SDK handler protocol
        raise ReauthRequiredError(
            f"MCP server {server_name!r} requires interactive login; run `/mcp login {server_name}`"
        )

    return callback


def has_stored_tokens(server_name: str) -> bool:
    """Return whether a stored token file exists for `server_name`.

    A cheap synchronous check for the connection wiring to decide whether to
    attach an OAuth provider. Fail-closed: an unsafe server name or any I/O
    error reports `False` (no usable stored token).

    Args:
        server_name: Configured MCP server name.

    Returns:
        `True` when a token file is present for the server.
    """
    try:
        return FileTokenStorage(server_name).path.exists()
    except (ValueError, OSError):
        return False
