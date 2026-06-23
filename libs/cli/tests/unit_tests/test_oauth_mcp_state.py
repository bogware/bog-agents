"""Tests for OAuth `state` validation in `exchange_code_for_token`.

Covers the CSRF / authorization-code-injection guard (P32): a mismatched or
missing `received_state` must be rejected *before* any token exchange, while a
matching state (and the default no-state path) must proceed.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest

from bog_agents_cli.oauth_mcp import (
    OAuthConfig,
    build_authorization_url,
    exchange_code_for_token,
)


def _make_config() -> OAuthConfig:
    return OAuthConfig(
        server_name="test-server",
        client_id="client-abc",
        client_secret="",
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        scopes=["read"],
        redirect_uri="http://localhost:8085/callback",
        use_pkce=True,
    )


class _FakeResponse:
    """Minimal urlopen stand-in returning a canned token JSON payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self) -> bytes:
        return self._buf.read()


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, calls: list[Any]) -> None:
    """Patch urllib.request.urlopen so no real network call is made."""
    import urllib.request

    def fake_urlopen(req: object, *args: object, **kwargs: object) -> _FakeResponse:
        calls.append(req)
        return _FakeResponse(
            {
                "access_token": "tok-123",
                "refresh_token": "refresh-123",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


async def test_mismatched_state_is_rejected_before_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A received_state that differs from expected_state must be rejected."""
    config = _make_config()
    calls: list[Any] = []
    _patch_urlopen(monkeypatch, calls)

    with pytest.raises(ValueError, match="state mismatch"):
        await exchange_code_for_token(
            config,
            "auth-code",
            "verifier",
            expected_state="the-real-state",
            received_state="attacker-state",
        )

    # No network exchange should have happened.
    assert calls == []


async def test_missing_received_state_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a state is expected, an empty received_state must be rejected."""
    config = _make_config()
    calls: list[Any] = []
    _patch_urlopen(monkeypatch, calls)

    with pytest.raises(ValueError, match="state mismatch"):
        await exchange_code_for_token(
            config,
            "auth-code",
            "verifier",
            expected_state="the-real-state",
            received_state="",
        )

    assert calls == []


async def test_matching_state_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching state must allow the exchange to proceed."""
    config = _make_config()
    calls: list[Any] = []
    _patch_urlopen(monkeypatch, calls)

    token = await exchange_code_for_token(
        config,
        "auth-code",
        "verifier",
        expected_state="the-real-state",
        received_state="the-real-state",
    )

    assert token.access_token == "tok-123"
    assert len(calls) == 1


async def test_default_no_state_preserves_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without expected_state, the check is skipped (backward compatible)."""
    config = _make_config()
    calls: list[Any] = []
    _patch_urlopen(monkeypatch, calls)

    token = await exchange_code_for_token(config, "auth-code", "verifier")

    assert token.access_token == "tok-123"
    assert len(calls) == 1


async def test_issued_state_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state from build_authorization_url is accepted when echoed back."""
    config = _make_config()
    _, issued_state, code_verifier = build_authorization_url(config)

    calls: list[Any] = []
    _patch_urlopen(monkeypatch, calls)

    token = await exchange_code_for_token(
        config,
        "auth-code",
        code_verifier,
        expected_state=issued_state,
        received_state=issued_state,
    )

    assert token.access_token == "tok-123"
    assert len(calls) == 1


def test_exchange_is_keyword_only_for_state() -> None:
    """expected_state / received_state must be keyword-only (public API)."""
    import inspect

    sig = inspect.signature(exchange_code_for_token)
    assert sig.parameters["expected_state"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["received_state"].kind is inspect.Parameter.KEYWORD_ONLY
    # Existing positional params remain positional-or-keyword.
    assert sig.parameters["code"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


# Guard against accidental loss of asyncio_mode = auto wiring locally.
assert asyncio is not None
