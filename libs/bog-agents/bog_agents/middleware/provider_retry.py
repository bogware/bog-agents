"""Bounded retry-with-backoff around provider model calls.

Wraps the model call (only — not tool calls, which may have side
effects) and retries on transient errors. Backoff is exponential with
jitter, bounded by ``max_attempts`` and ``max_delay``.

Defaults are deliberately conservative: 3 attempts (1 try + 2 retries),
1s initial delay, 16s max delay, 2x multiplier. The retry policy is
configurable per-instance.

What we retry on:

- ``anthropic.APITimeoutError`` / ``APIConnectionError`` / 5xx
- ``openai.APITimeoutError`` / ``APIConnectionError`` / 5xx
- Generic ``TimeoutError`` / ``ConnectionError``

What we DON'T retry on:

- 4xx errors (auth, rate-limit-overshoot, bad-request — retrying won't help)
- ``KeyboardInterrupt`` / ``SystemExit`` / ``CancelledError``

Retries are emitted as ``provider.retry`` structured-log events when
the CLI's observability module is importable; otherwise they are
silent except for a warning log line.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

logger = logging.getLogger(__name__)


# Default policy. Mirror constants are in libs/cli/bog_agents_cli/_constants.py;
# the SDK avoids importing CLI-only modules.
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_INITIAL_DELAY_S = 1.0
_DEFAULT_MAX_DELAY_S = 16.0
_DEFAULT_BACKOFF_FACTOR = 2.0


def _is_retryable(exc: BaseException) -> bool:
    """Heuristically classify an exception as retryable.

    Strategy: name-match against known provider error classes (so we
    don't have to import anthropic/openai unconditionally), with a
    fallback for generic networking exceptions.
    """
    name = type(exc).__name__
    # Provider-specific transient errors.
    transient_names = {
        "APITimeoutError",
        "APIConnectionError",
        "APIServiceUnavailableError",
        "InternalServerError",
        "ServiceUnavailableError",
        "OverloadedError",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadTimeoutError",
        "RemoteProtocolError",
    }
    if name in transient_names:
        return True
    # 5xx HTTP status if the exception carries one.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    # Generic stdlib networking exceptions.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return False


def _compute_delay(
    attempt: int,
    *,
    initial_s: float,
    factor: float,
    max_s: float,
) -> float:
    """Exponential backoff with full jitter, bounded by ``max_s``."""
    raw = initial_s * (factor**attempt)
    capped = min(raw, max_s)
    # Full jitter: pick a uniform value in [0, capped]. This avoids
    # synchronized retry storms across multiple concurrent agents.
    return random.uniform(0, capped)


def _emit_retry(*, attempt: int, delay_s: float, error_type: str, model: str | None) -> None:
    """Best-effort structured log; never raises."""
    try:
        # Lazy CLI import: when running inside the CLI process we get
        # registry-backed metrics for free. SDK-only callers (e.g.
        # someone importing bog-agents from a notebook) get plain logs.
        from bog_agents_cli._observability import EVT_PROVIDER_RETRY, log_event

        log_event(
            EVT_PROVIDER_RETRY,
            label=model or "unknown",
            attempt=attempt,
            delay_ms=int(delay_s * 1000),
            error_type=error_type,
        )
    except Exception:
        logger.warning(
            "provider retry: attempt=%d delay=%.2fs error=%s model=%s",
            attempt,
            delay_s,
            error_type,
            model,
        )


__all__ = [
    "ProviderRetryMiddleware",
]


class ProviderRetryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Retry transient provider failures with bounded exponential backoff.

    Use::

        agent = create_agent(
            model="claude-sonnet-4-6",
            middleware=[ProviderRetryMiddleware()],
        )

    Or with a custom policy::

        agent = create_agent(
            middleware=[ProviderRetryMiddleware(max_attempts=5, initial_delay_s=0.5)],
        )

    Tool calls are NOT retried by this middleware — they may have side
    effects, and retrying after a partially-applied effect is a worse
    outcome than reporting the failure to the agent.
    """

    def __init__(
        self,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        initial_delay_s: float = _DEFAULT_INITIAL_DELAY_S,
        max_delay_s: float = _DEFAULT_MAX_DELAY_S,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        super().__init__()
        if max_attempts < 1:
            msg = f"max_attempts must be >= 1, got {max_attempts}"
            raise ValueError(msg)
        self._max_attempts = max_attempts
        self._initial_delay_s = max(0.0, initial_delay_s)
        self._max_delay_s = max(self._initial_delay_s, max_delay_s)
        self._backoff_factor = max(1.0, backoff_factor)

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,
    ) -> ModelResponse:
        return await self._call_with_retries(request, call_next)

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,
    ) -> ModelResponse:
        # Sync path: retry without backoff (we'd need an event loop to
        # await asyncio.sleep). Use ``time.sleep`` instead.
        attempt = 0
        last_exc: BaseException | None = None
        model_name = self._extract_model_name(request)
        while attempt < self._max_attempts:
            try:
                return call_next(request)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                if not _is_retryable(exc):
                    raise
                last_exc = exc
                attempt += 1
                if attempt >= self._max_attempts:
                    break
                delay = _compute_delay(
                    attempt - 1,
                    initial_s=self._initial_delay_s,
                    factor=self._backoff_factor,
                    max_s=self._max_delay_s,
                )
                _emit_retry(
                    attempt=attempt,
                    delay_s=delay,
                    error_type=type(exc).__name__,
                    model=model_name,
                )
                time.sleep(delay)
        # Exhausted retries — re-raise the last exception.
        assert last_exc is not None
        raise last_exc

    async def _call_with_retries(
        self,
        request: ModelRequest,
        call_next: Any,
    ) -> ModelResponse:
        attempt = 0
        last_exc: BaseException | None = None
        model_name = self._extract_model_name(request)
        while attempt < self._max_attempts:
            try:
                return await call_next(request)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as exc:
                if not _is_retryable(exc):
                    raise
                last_exc = exc
                attempt += 1
                if attempt >= self._max_attempts:
                    break
                delay = _compute_delay(
                    attempt - 1,
                    initial_s=self._initial_delay_s,
                    factor=self._backoff_factor,
                    max_s=self._max_delay_s,
                )
                _emit_retry(
                    attempt=attempt,
                    delay_s=delay,
                    error_type=type(exc).__name__,
                    model=model_name,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _extract_model_name(request: Any) -> str | None:
        """Best-effort extract a model name from the request for logging."""
        for attr in ("model", "model_name"):
            value = getattr(request, attr, None)
            if isinstance(value, str):
                return value
            inner = getattr(value, "model", None)
            if isinstance(inner, str):
                return inner
        return None
