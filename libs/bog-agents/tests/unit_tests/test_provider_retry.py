"""Unit tests for ProviderRetryMiddleware."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bog_agents.middleware.provider_retry import (
    ProviderRetryMiddleware,
    _compute_delay,
    _is_retryable,
)


class _FakeAPITimeout(Exception):
    """Mimics anthropic.APITimeoutError by class name."""


_FakeAPITimeout.__name__ = "APITimeoutError"


class _FakeOverloaded(Exception):
    pass


_FakeOverloaded.__name__ = "OverloadedError"


class _FakeAuthError(Exception):
    """4xx — not retryable."""


_FakeAuthError.__name__ = "AuthenticationError"


class _FakeWith5xx(Exception):
    def __init__(self) -> None:
        super().__init__("server error")
        self.status_code = 503


class TestIsRetryable:
    def test_anthropic_timeout_class_name_is_retryable(self):
        assert _is_retryable(_FakeAPITimeout())

    def test_overloaded_is_retryable(self):
        assert _is_retryable(_FakeOverloaded())

    def test_4xx_auth_not_retryable(self):
        assert not _is_retryable(_FakeAuthError())

    def test_5xx_status_code_retryable(self):
        assert _is_retryable(_FakeWith5xx())

    def test_stdlib_timeout_retryable(self):
        assert _is_retryable(TimeoutError())

    def test_connection_error_retryable(self):
        assert _is_retryable(ConnectionError())

    def test_value_error_not_retryable(self):
        assert not _is_retryable(ValueError("bad input"))


class TestBackoff:
    def test_jitter_is_within_bounds(self):
        for _ in range(50):
            delay = _compute_delay(0, initial_s=1.0, factor=2.0, max_s=16.0)
            assert 0 <= delay <= 1.0

    def test_capped_at_max(self):
        # attempt=10 with factor=2 → raw delay would be 1024s; cap is 16.
        for _ in range(20):
            delay = _compute_delay(10, initial_s=1.0, factor=2.0, max_s=16.0)
            assert 0 <= delay <= 16.0


class TestRetryMiddleware:
    def test_init_validates_max_attempts(self):
        with pytest.raises(ValueError, match="max_attempts"):
            ProviderRetryMiddleware(max_attempts=0)

    def test_clamps_initial_delay_negative_to_zero(self):
        m = ProviderRetryMiddleware(initial_delay_s=-5.0)
        assert m._initial_delay_s == 0.0

    async def test_retries_on_retryable_error_then_succeeds(self):
        mw = ProviderRetryMiddleware(max_attempts=3, initial_delay_s=0.001, max_delay_s=0.01)
        attempts = {"count": 0}

        async def call_next(_request: Any, _runtime: Any) -> str:
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise _FakeAPITimeout
            return "ok"

        result = await mw.awrap_model_call(object(), call_next, object())
        assert result == "ok"
        assert attempts["count"] == 2

    async def test_does_not_retry_non_retryable(self):
        mw = ProviderRetryMiddleware(max_attempts=5, initial_delay_s=0.001)
        attempts = {"count": 0}

        async def call_next(_request: Any, _runtime: Any) -> str:
            attempts["count"] += 1
            msg = "bad key"
            raise _FakeAuthError(msg)

        with pytest.raises(_FakeAuthError):
            await mw.awrap_model_call(object(), call_next, object())
        assert attempts["count"] == 1

    async def test_exhausts_attempts_then_raises(self):
        mw = ProviderRetryMiddleware(max_attempts=3, initial_delay_s=0.001, max_delay_s=0.01)
        attempts = {"count": 0}

        async def call_next(_request: Any, _runtime: Any) -> str:
            attempts["count"] += 1
            raise _FakeAPITimeout

        with pytest.raises(_FakeAPITimeout):
            await mw.awrap_model_call(object(), call_next, object())
        assert attempts["count"] == 3

    async def test_keyboard_interrupt_passes_through(self):
        mw = ProviderRetryMiddleware(max_attempts=5, initial_delay_s=0.001)

        async def call_next(_request: Any, _runtime: Any) -> str:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            await mw.awrap_model_call(object(), call_next, object())

    async def test_cancelled_error_passes_through(self):
        mw = ProviderRetryMiddleware(max_attempts=5, initial_delay_s=0.001)

        async def call_next(_request: Any, _runtime: Any) -> str:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await mw.awrap_model_call(object(), call_next, object())

    def test_sync_path_retries(self):
        mw = ProviderRetryMiddleware(max_attempts=3, initial_delay_s=0.001, max_delay_s=0.01)
        attempts = {"count": 0}

        def call_next(_request: Any, _runtime: Any) -> str:
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise _FakeAPITimeout
            return "ok"

        result = mw.wrap_model_call(object(), call_next, object())
        assert result == "ok"
        assert attempts["count"] == 2

    def test_extract_model_name_from_request_attr(self):
        class Req:
            model = "claude-sonnet-4-6"

        assert ProviderRetryMiddleware._extract_model_name(Req()) == "claude-sonnet-4-6"

    def test_extract_model_name_returns_none_when_absent(self):
        assert ProviderRetryMiddleware._extract_model_name(object()) is None
