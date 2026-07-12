"""Tests for `bog_agents_cli.mcp_auth` — connection-time OAuth resolution.

Covers `_resolve_mcp_auth` (provider for opt-in / stored-token remote servers,
`None` for stdio / static-header / no-auth), the `needs_oauth_login` pre-check,
and the pure 401-challenge detectors. No network: the OAuth provider factory is
patched to a sentinel so no loopback socket is opened, and 401 challenges are
synthesized with `httpx` objects.
"""

from __future__ import annotations

import httpx
import pytest

from bog_agents_cli import mcp_oauth
from bog_agents_cli.mcp_auth import (
    _resolve_mcp_auth,
    auth_login_hint,
    find_oauth_challenge,
    is_auth_challenge,
    needs_oauth_login,
)


class _SentinelAuth(httpx.Auth):
    """Stand-in httpx.Auth returned by the patched provider factory."""

    def __init__(self, server_name: str, url: str) -> None:
        self.server_name = server_name
        self.url = url


@pytest.fixture
def _oauth_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Redirect token storage to a temp dir and stub the provider factory.

    The stub avoids binding a real loopback callback socket and lets a test
    assert that `_resolve_mcp_auth` took the build-a-provider branch.
    """
    monkeypatch.setattr(mcp_oauth, "default_oauth_dir", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "bog_agents_cli.mcp_token_storage.default_oauth_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        mcp_oauth,
        "build_oauth_provider",
        lambda name, url, **_kw: _SentinelAuth(name, url),
    )
    return tmp_path


def test_resolve_returns_provider_for_auth_oauth(_oauth_env) -> None:
    """A server declaring `auth: oauth` gets a provider (the factory's result)."""
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp", "auth": "oauth"}
    auth = _resolve_mcp_auth("srv", cfg)
    assert isinstance(auth, _SentinelAuth)
    assert auth.url == "https://mcp.example.com/mcp"


def test_resolve_returns_provider_when_tokens_stored(_oauth_env) -> None:
    """A remote server with a stored token gets a provider even without opt-in."""
    (_oauth_env / "srv.json").write_text("{}", encoding="utf-8")
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp"}
    assert isinstance(_resolve_mcp_auth("srv", cfg), _SentinelAuth)


def test_resolve_sse_transport(_oauth_env) -> None:
    """SSE remote servers are also OAuth-capable."""
    cfg = {"transport": "sse", "url": "https://mcp.example.com/sse", "auth": "oauth"}
    assert isinstance(_resolve_mcp_auth("srv", cfg), _SentinelAuth)


def test_resolve_none_for_stdio(_oauth_env) -> None:
    """Stdio servers never carry OAuth."""
    cfg = {"command": "npx", "args": ["-y", "pkg"], "auth": "oauth"}
    assert _resolve_mcp_auth("srv", cfg) is None


def test_resolve_none_for_static_header(_oauth_env) -> None:
    """A static Authorization header takes precedence — no provider."""
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": "oauth",
        "headers": {"Authorization": "Bearer ${TOKEN}"},
    }
    assert _resolve_mcp_auth("srv", cfg) is None


def test_resolve_static_header_case_insensitive(_oauth_env) -> None:
    """Header name matching for Authorization is case-insensitive."""
    (_oauth_env / "srv.json").write_text("{}", encoding="utf-8")
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"authorization": "Bearer x"},
    }
    assert _resolve_mcp_auth("srv", cfg) is None


def test_resolve_none_for_remote_without_auth(_oauth_env) -> None:
    """A remote server with no opt-in and no stored token is unchanged."""
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp"}
    assert _resolve_mcp_auth("srv", cfg) is None


def test_resolve_none_for_missing_url(_oauth_env) -> None:
    """A remote server config without a usable URL yields no provider."""
    cfg = {"type": "http", "auth": "oauth"}
    assert _resolve_mcp_auth("srv", cfg) is None


def test_needs_oauth_login_true_without_token(_oauth_env) -> None:
    """An opted-in remote server with no stored token needs a login."""
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp", "auth": "oauth"}
    assert needs_oauth_login("srv", cfg) is True


def test_needs_oauth_login_false_with_token(_oauth_env) -> None:
    """Once a token is stored, no upfront login is needed."""
    (_oauth_env / "srv.json").write_text("{}", encoding="utf-8")
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp", "auth": "oauth"}
    assert needs_oauth_login("srv", cfg) is False


def test_needs_oauth_login_false_without_opt_in(_oauth_env) -> None:
    """A server that did not opt into OAuth never needs an upfront login."""
    cfg = {"type": "http", "url": "https://mcp.example.com/mcp"}
    assert needs_oauth_login("srv", cfg) is False


def test_needs_oauth_login_false_for_stdio(_oauth_env) -> None:
    """Stdio servers never need OAuth login."""
    cfg = {"command": "npx", "auth": "oauth"}
    assert needs_oauth_login("srv", cfg) is False


def test_needs_oauth_login_false_with_static_header(_oauth_env) -> None:
    """A static Authorization header means no interactive login is required."""
    cfg = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": "oauth",
        "headers": {"Authorization": "Bearer x"},
    }
    assert needs_oauth_login("srv", cfg) is False


def _make_401(*, www_authenticate: str | None = None) -> httpx.HTTPStatusError:
    """Build an httpx 401 HTTPStatusError, optionally with a challenge header."""
    request = httpx.Request("GET", "https://mcp.example.com/mcp")
    headers = {}
    if www_authenticate is not None:
        headers["WWW-Authenticate"] = www_authenticate
    response = httpx.Response(401, headers=headers, request=request)
    return httpx.HTTPStatusError("Unauthorized", request=request, response=response)


def test_is_auth_challenge_direct() -> None:
    """A bare 401 HTTPStatusError is detected as an auth challenge."""
    assert is_auth_challenge(_make_401()) is True


def test_is_auth_challenge_nested_in_group() -> None:
    """A 401 nested inside an ExceptionGroup is still detected."""
    group = ExceptionGroup("boom", [_make_401()])
    assert is_auth_challenge(group) is True


def test_is_auth_challenge_via_cause() -> None:
    """A 401 reachable through __cause__ is detected."""
    exc = RuntimeError("startup failed")
    exc.__cause__ = _make_401()
    assert is_auth_challenge(exc) is True


def test_is_auth_challenge_false_for_500() -> None:
    """A non-401 HTTP error is not an auth challenge."""
    request = httpx.Request("GET", "https://mcp.example.com/mcp")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert is_auth_challenge(exc) is False


def test_is_auth_challenge_false_for_plain_error() -> None:
    """A plain exception with no HTTP status is not an auth challenge."""
    assert is_auth_challenge(RuntimeError("nope")) is False


def test_find_oauth_challenge_parses_resource_metadata() -> None:
    """The RFC 9728 resource_metadata URL is extracted from the challenge."""
    meta = "https://mcp.example.com/.well-known/oauth-protected-resource"
    exc = _make_401(www_authenticate=f'Bearer resource_metadata="{meta}"')
    assert find_oauth_challenge(exc) == meta


def test_find_oauth_challenge_none_without_bearer() -> None:
    """A non-Bearer challenge yields no resource_metadata URL."""
    exc = _make_401(www_authenticate='Basic realm="x"')
    assert find_oauth_challenge(exc) is None


def test_find_oauth_challenge_none_without_challenge() -> None:
    """A 401 with no WWW-Authenticate header yields no metadata URL."""
    assert find_oauth_challenge(_make_401()) is None


def test_auth_login_hint_names_server_and_command() -> None:
    """The hint is actionable: it names the server and the login command."""
    hint = auth_login_hint("myserver")
    assert "myserver" in hint
    assert "/mcp login myserver" in hint
