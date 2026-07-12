"""Connection-time OAuth resolution and 401-challenge detection for MCP.

Pure glue between the MCP server config and the spec-compliant OAuth provider
built by `mcp_oauth`. It answers two questions the connection loader in
`mcp_tools` asks per remote server:

- *Should this connection carry an OAuth provider?* — `_resolve_mcp_auth`
    returns an `httpx.Auth` (an `mcp.client.auth.OAuthClientProvider`) when the
    server opted into OAuth (`"auth": "oauth"`) or already has stored tokens,
    and `None` for stdio, static-`Authorization`-header, and no-auth servers so
    those paths are left untouched.
- *Did a load-time failure mean "you need to authenticate"?* —
    `is_auth_challenge` walks an exception tree for an HTTP 401 (the MCP
    authorization spec's unauthenticated response, RFC 9728) so an opaque
    startup timeout becomes an actionable "run `/mcp login <server>`" message.

Nothing here performs I/O beyond the cheap synchronous `has_stored_tokens`
check delegated to `mcp_oauth`; the actual OAuth handshake lives in the SDK
provider. Importing this module must never pull in `mcp_tools` (the reverse
dependency), so transport classification is inlined rather than imported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable

_REMOTE_TRANSPORTS = frozenset({"sse", "http"})
"""Transport types that speak to a remote URL and can carry OAuth."""

_HTTP_UNAUTHORIZED = 401
"""HTTP status a server returns for an unauthenticated MCP request (RFC 9728)."""


def _server_transport(server_config: dict[str, Any]) -> str:
    """Return the transport for a server config.

    Mirrors `mcp_tools._resolve_server_type` (the `type` key wins over
    `transport`, defaulting to `stdio`) without importing `mcp_tools`, which
    would create an import cycle.

    Args:
        server_config: Server configuration dictionary.

    Returns:
        Transport type string (`stdio`, `sse`, or `http`).
    """
    explicit = server_config.get("type")
    if explicit is not None:
        return str(explicit)
    return str(server_config.get("transport", "stdio"))


def _has_authorization_header(server_config: dict[str, Any]) -> bool:
    """Return whether the server config sets a static `Authorization` header.

    A static header is the user's explicit credential choice and takes
    precedence over OAuth, so such servers are left as-is (no provider).

    Args:
        server_config: Server configuration dictionary.

    Returns:
        `True` when a header named `Authorization` (any case) is configured.
    """
    headers = server_config.get("headers") or {}
    if not isinstance(headers, dict):
        return False
    return any(str(name).lower() == "authorization" for name in headers)


def _resolve_mcp_auth(
    server_name: str,
    server_config: dict[str, Any],
) -> httpx.Auth | None:
    """Return an OAuth provider for a server that should authenticate, else None.

    The connection loader in `mcp_tools` attaches the returned value as the
    remote connection's `auth` so the MCP SDK performs discovery, Dynamic
    Client Registration, PKCE, the token exchange, and automatic refresh.

    A provider is returned only for a remote (`sse`/`http`) server that has no
    static `Authorization` header and either opted into OAuth (`"auth":
    "oauth"`) or already has stored tokens. stdio servers, static-header
    servers, and remote servers with no OAuth signal return `None` so their
    connections are unchanged.

    Args:
        server_name: Configured MCP server name (used for token storage).
        server_config: Server configuration dictionary.

    Returns:
        An `httpx.Auth` (`OAuthClientProvider`) to attach, or `None`.
    """
    if _server_transport(server_config) not in _REMOTE_TRANSPORTS:
        return None
    if _has_authorization_header(server_config):
        return None
    url = server_config.get("url")
    if not url or not isinstance(url, str):
        return None

    from bog_agents_cli.mcp_oauth import build_oauth_provider, has_stored_tokens

    if server_config.get("auth") == "oauth" or has_stored_tokens(server_name):
        # Connection/refresh path: never bind a loopback or open a browser here.
        # A missing/unrefreshable token surfaces via the /mcp login hint instead.
        return build_oauth_provider(server_name, url, interactive=False)
    return None


def needs_oauth_login(server_name: str, server_config: dict[str, Any]) -> bool:
    """Return whether a server opted into OAuth but has no stored token yet.

    Such a server cannot connect non-interactively: attaching a provider at
    load time would drive a browser (blocking) or time out opaquely. The loader
    uses this to skip the server and surface an actionable login hint instead.

    Args:
        server_name: Configured MCP server name.
        server_config: Server configuration dictionary.

    Returns:
        `True` when the server sets `"auth": "oauth"`, is remote, carries no
            static `Authorization` header, and has no stored token.
    """
    if server_config.get("auth") != "oauth":
        return False
    if _server_transport(server_config) not in _REMOTE_TRANSPORTS:
        return False
    if _has_authorization_header(server_config):
        return False

    from bog_agents_cli.mcp_oauth import has_stored_tokens

    return not has_stored_tokens(server_name)


def auth_login_hint(server_name: str) -> str:
    """Return an actionable "please authenticate" message naming the server.

    Args:
        server_name: Configured MCP server name.

    Returns:
        A one-line hint pointing the user at the `/mcp login` command.
    """
    return (
        f"server '{server_name}' requires authentication — "
        f"run `/mcp login {server_name}` to sign in"
    )


def _iter_exception_tree(exc: BaseException) -> Iterable[BaseException]:
    """Yield every exception reachable from `exc` without revisiting a node.

    Walks `ExceptionGroup` members plus each exception's `__cause__` and
    `__context__`, tracking visited object ids so a cyclic chain terminates.

    Args:
        exc: Root exception to traverse.

    Yields:
        Each distinct exception in the tree.
    """
    visited: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            stack.append(cause)


def is_auth_challenge(exc: BaseException) -> bool:
    """Return whether `exc`'s tree carries an HTTP 401 (authentication needed).

    Per the MCP authorization spec (RFC 9728), a server requiring OAuth answers
    an unauthenticated request with HTTP 401. The MCP client surfaces that as an
    `httpx.HTTPStatusError`, often nested inside an `ExceptionGroup`. Detecting
    it lets the loader turn an opaque startup failure into an actionable
    "run `/mcp login <server>`" message.

    Args:
        exc: Root exception raised during tool loading.

    Returns:
        `True` when a 401 response is found anywhere in the tree.
    """
    for current in _iter_exception_tree(exc):
        if isinstance(current, httpx.HTTPStatusError):
            response = current.response
            if response is not None and response.status_code == _HTTP_UNAUTHORIZED:
                return True
    return False


_BEARER_SCHEME_RE = None
"""Lazily-compiled `Bearer` scheme matcher (set on first `find_oauth_challenge`)."""

_RESOURCE_METADATA_RE = None
"""Lazily-compiled `resource_metadata=` matcher."""


def _oauth_resource_challenge(headers: httpx.Headers) -> str | None:
    """Return the RFC 9728 `resource_metadata` URL from a Bearer 401 challenge.

    A single `WWW-Authenticate` header may list several comma-separated
    challenges (RFC 7235), and a response may repeat the header. Every value is
    scanned for a `Bearer` scheme that advertises a `resource_metadata`
    parameter.

    Args:
        headers: Response headers to inspect.

    Returns:
        The `resource_metadata` URL when a Bearer challenge carries one, else
            `None`.
    """
    global _BEARER_SCHEME_RE, _RESOURCE_METADATA_RE  # noqa: PLW0603  # one-time regex cache
    if _BEARER_SCHEME_RE is None or _RESOURCE_METADATA_RE is None:
        import re

        _BEARER_SCHEME_RE = re.compile(r"(?:^|,)\s*bearer\b", re.IGNORECASE)
        _RESOURCE_METADATA_RE = re.compile(
            r'(?:^|[\s,])resource_metadata\s*=\s*"?([^",\s]+)',
            re.IGNORECASE,
        )

    for value in headers.get_list("www-authenticate"):
        if _BEARER_SCHEME_RE.search(value) is None:
            continue
        match = _RESOURCE_METADATA_RE.search(value)
        if match is not None:
            return match.group(1)
    return None


def find_oauth_challenge(exc: BaseException) -> str | None:
    """Return the `resource_metadata` URL of a 401 OAuth challenge in `exc`.

    Per RFC 9728, a server requiring OAuth answers with HTTP 401 plus a Bearer
    `WWW-Authenticate` challenge pointing at its protected-resource metadata.
    Walks the whole exception tree (see `is_auth_challenge`) and returns the
    advertised metadata URL from the first such challenge.

    Args:
        exc: Root exception to inspect.

    Returns:
        The `resource_metadata` URL when a 401 Bearer challenge advertising one
            is found, else `None`.
    """
    for current in _iter_exception_tree(exc):
        if isinstance(current, httpx.HTTPStatusError):
            response = current.response
            if response is not None and response.status_code == _HTTP_UNAUTHORIZED:
                challenge = _oauth_resource_challenge(response.headers)
                if challenge is not None:
                    return challenge
    return None
