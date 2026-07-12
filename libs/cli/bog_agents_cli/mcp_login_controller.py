"""Orchestrate MCP OAuth login / logout / status without the TUI.

Thin, testable controller over `mcp_oauth` + `mcp_token_storage`. `login`
drives exactly one authorization: it starts a loopback callback server, builds
the spec-compliant `OAuthClientProvider`, and exercises it against the server
so the MCP SDK performs discovery, Dynamic Client Registration, PKCE, and the
token exchange, persisting the result through `FileTokenStorage`. All the
external effects — opening a browser, the loopback server, and the handshake
that actually hits the network — are injectable so the flow is exercisable
without a real browser or network.

Nothing here logs, returns, or otherwise surfaces a token value: results carry
only structural facts (whether a token is stored, its expiry).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bog_agents_cli.mcp_loopback import (
    LoopbackCallbackError,
    LoopbackCallbackServer,
    LoopbackTimeoutError,
)
from bog_agents_cli.mcp_oauth import BrowserOpener, build_oauth_provider
from bog_agents_cli.mcp_token_storage import FileTokenStorage, is_safe_server_name

if TYPE_CHECKING:
    from mcp.client.auth import OAuthClientProvider

logger = logging.getLogger(__name__)

Handshake = Callable[..., Awaitable[None]]
"""Drives the provider against the server to complete the OAuth handshake.

Called as `handshake(provider=..., server_name=..., server_url=...,
transport=...)`. The default connects a one-shot MCP session; tests inject a
stand-in that persists a token without touching the network.
"""

LoopbackFactory = Callable[[], LoopbackCallbackServer]
ProviderFactory = Callable[..., "OAuthClientProvider"]


@dataclass(frozen=True)
class LoginResult:
    """Outcome of a `login` attempt. Never carries a token value."""

    ok: bool
    """Whether a token was obtained and persisted."""

    server_name: str
    """The MCP server the login targeted."""

    message: str
    """User-facing, token-safe summary of the outcome."""

    expires_at: float | None = None
    """Absolute access-token expiry (Unix epoch), when known and successful."""


async def _default_handshake(
    *,
    provider: OAuthClientProvider,
    server_name: str,
    server_url: str,
    transport: str,
) -> None:
    """Open a one-shot MCP session so the provider drives the OAuth flow.

    Attaching the provider as the connection's `auth` makes the MCP SDK run the
    full discovery -> DCR -> PKCE -> exchange handshake the first time the
    session connects, persisting tokens through the provider's storage.

    Raises:
        RuntimeError: If `transport` is not a remote http/sse transport.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.sessions import (
        SSEConnection,
        StreamableHttpConnection,
    )

    conn: SSEConnection | StreamableHttpConnection
    if transport == "http":
        conn = StreamableHttpConnection(
            transport="streamable_http",
            url=server_url,
            auth=provider,  # ty: ignore[invalid-argument-type]
        )
    elif transport == "sse":
        conn = SSEConnection(
            transport="sse",
            url=server_url,
            auth=provider,  # ty: ignore[invalid-argument-type]
        )
    else:
        msg = (
            f"MCP server {server_name!r} uses {transport!r} transport; OAuth "
            "login is only valid for http/sse servers."
        )
        raise RuntimeError(msg)

    client = MultiServerMCPClient(connections={server_name: conn})
    async with client.session(server_name):
        pass


def _safe_error(exc: BaseException) -> str:
    """Return a token-safe one-line summary of a login failure.

    OAuth failures can surface as `ExceptionGroup`s or SDK errors whose
    `repr`/`args` may embed an `OAuthToken`. Only our own exception types
    (whose messages we control) are rendered verbatim; anything else degrades
    to the chain of exception class names so a token can never leak.

    Args:
        exc: The raised exception.

    Returns:
        A short, token-free description.
    """
    safe_types = (
        LoopbackTimeoutError,
        LoopbackCallbackError,
        ValueError,
    )
    if isinstance(exc, safe_types):
        return str(exc)

    parts: list[str] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        parts.append(type(current).__name__)
        if isinstance(current, BaseExceptionGroup):
            inner = ", ".join(type(e).__name__ for e in current.exceptions[:5])
            parts.append(f"[{inner}]")
            break
        current = current.__cause__ or current.__context__
    return " -> ".join(parts) if parts else type(exc).__name__


async def login(
    server_name: str,
    server_url: str,
    *,
    transport: str = "http",
    scope: str | None = None,
    timeout: float = 300.0,  # noqa: ASYNC109  # passed through to the loopback wait, not an asyncio.timeout
    open_browser: BrowserOpener | None = None,
    loopback_factory: LoopbackFactory | None = None,
    provider_factory: ProviderFactory | None = None,
    handshake: Handshake | None = None,
) -> LoginResult:
    """Drive one OAuth authorization for `server_name`, persisting tokens.

    Args:
        server_name: Configured MCP server name (also the token-file stem).
        server_url: Remote MCP server URL to authenticate against.
        transport: `"http"` or `"sse"`. Passed to the default handshake.
        scope: Optional space-delimited scope string to request.
        timeout: Seconds to wait for the OAuth callback.
        open_browser: Browser-opening callable (default `webbrowser.open`).
            Injectable so tests avoid launching a real browser.
        loopback_factory: Builds the loopback callback server. Injectable for
            tests; defaults to a fresh ephemeral-port `LoopbackCallbackServer`.
        provider_factory: Builds the `OAuthClientProvider`. Defaults to
            `mcp_oauth.build_oauth_provider`.
        handshake: Drives the provider to completion. Defaults to a one-shot
            MCP session connect; tests inject a network-free stand-in.

    Returns:
        A `LoginResult` describing success or a token-safe failure. This
            function does not raise for expected failures.
    """
    if not is_safe_server_name(server_name):
        return LoginResult(
            ok=False,
            server_name=server_name,
            message=(
                f"Invalid MCP server name {server_name!r}: names must contain "
                "only letters, digits, '_', '-', '.'."
            ),
        )

    storage = FileTokenStorage(server_name)
    make_loopback = loopback_factory or LoopbackCallbackServer
    make_provider = provider_factory or build_oauth_provider
    drive = handshake or _default_handshake

    try:
        loopback = make_loopback()
    except OSError as exc:
        return LoginResult(
            ok=False,
            server_name=server_name,
            message=f"Could not start the local OAuth callback server: {exc}",
        )

    try:
        provider = make_provider(
            server_name,
            server_url,
            storage=storage,
            loopback=loopback,
            open_browser=open_browser,
            scope=scope,
            timeout=timeout,
        )
        await drive(
            provider=provider,
            server_name=server_name,
            server_url=server_url,
            transport=transport,
        )
    except (
        LoopbackTimeoutError,
        LoopbackCallbackError,
        OSError,
        RuntimeError,
        ValueError,
        BaseExceptionGroup,
    ) as exc:
        return LoginResult(
            ok=False,
            server_name=server_name,
            message=f"Login to MCP server '{server_name}' failed: {_safe_error(exc)}",
        )
    finally:
        loopback.close()

    if not storage.stored_token_present():
        return LoginResult(
            ok=False,
            server_name=server_name,
            message=(
                f"Login to MCP server '{server_name}' completed without "
                "persisting a token. Try again."
            ),
        )

    return LoginResult(
        ok=True,
        server_name=server_name,
        message=f"Logged in to MCP server '{server_name}'.",
        expires_at=storage.stored_expires_at(),
    )


def logout(server_name: str) -> bool:
    """Delete the stored token file for `server_name`.

    Args:
        server_name: Configured MCP server name.

    Returns:
        `True` if a stored token file was removed, `False` if there was none
            (or the name is unsafe / the unlink failed).
    """
    if not is_safe_server_name(server_name):
        return False
    return FileTokenStorage(server_name).delete()


def status(server_name: str) -> dict[str, object]:
    """Report the stored-token status for `server_name` (no secret).

    Args:
        server_name: Configured MCP server name.

    Returns:
        A dict with `server_name`, `has_token` (bool), `expires_at`
            (Unix-epoch float or `None`), and `expired` (bool or `None` when
            the expiry is unknown).
    """
    import time

    if not is_safe_server_name(server_name):
        return {
            "server_name": server_name,
            "has_token": False,
            "expires_at": None,
            "expired": None,
        }
    storage = FileTokenStorage(server_name)
    has_token = storage.stored_token_present()
    expires_at = storage.stored_expires_at() if has_token else None
    expired: bool | None = None if expires_at is None else expires_at <= time.time()
    return {
        "server_name": server_name,
        "has_token": has_token,
        "expires_at": expires_at,
        "expired": expired,
    }
