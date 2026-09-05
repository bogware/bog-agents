"""Provider-agnostic failover for rate limits and quota exhaustion (ROADMAP #53).

The CLI's `BedrockResilienceMiddleware` categorises AWS failures and hops to a
sibling model. This is the vendor-neutral half: when a model call fails with a
rate limit (429 / 529, "overloaded", "quota"), rotate through the configured
fallback specs (a local Ollama model included), remember the alternate that
worked, and *park* the primary until the provider's reset header — or a
cooldown — says to try it again. Everything is pure logic around two injected
callables, `build_model(spec)` and the clock, so it unit-tests without a
provider. `state.describe()` is the one line the TUI and the daemon show:
"parked (rate_limit) until 14:07, answering with ollama:qwen3".
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

FailureKind = Literal["rate_limit", "quota", "overloaded", "unavailable"]

_STATUS_KINDS: dict[int, FailureKind] = {429: "rate_limit", 529: "overloaded", 503: "unavailable", 502: "unavailable", 402: "quota"}
_TYPE_KINDS: dict[str, FailureKind] = {
    "RateLimitError": "rate_limit",
    "ResourceExhausted": "quota",
    "InsufficientQuotaError": "quota",
    "OverloadedError": "overloaded",
    "ServiceUnavailable": "unavailable",
    "ServiceUnavailableError": "unavailable",
}
_SUBSTRING_KINDS: tuple[tuple[str, FailureKind], ...] = (
    ("insufficient_quota", "quota"),
    ("resource exhausted", "quota"),
    ("resource_exhausted", "quota"),
    ("quota", "quota"),
    ("billing", "quota"),
    ("rate limit", "rate_limit"),
    ("rate_limit", "rate_limit"),
    ("ratelimit", "rate_limit"),
    ("too many requests", "rate_limit"),
    ("throttl", "rate_limit"),
    ("overloaded", "overloaded"),
    ("at capacity", "overloaded"),
    ("service unavailable", "unavailable"),
    ("temporarily unavailable", "unavailable"),
)
_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$")
_RESET_HEADERS = (
    "retry-after",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-reset",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-reset",
)
_MAX_PARK_SECONDS = 6 * 3600.0


def _status_code(exc: BaseException) -> int | None:
    for candidate in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None), getattr(exc, "code", None)):
        if isinstance(candidate, int) and 100 <= candidate <= 599:
            return candidate
    return None


def classify_failure(exc: BaseException) -> FailureKind | None:
    """Which failover-worthy failure `exc` is, or `None` for anything else (bugs propagate)."""
    code = _status_code(exc)
    if code in _STATUS_KINDS:
        return _STATUS_KINDS[code]
    for klass in type(exc).__mro__:
        if klass.__name__ in _TYPE_KINDS:
            return _TYPE_KINDS[klass.__name__]
    low = str(exc).lower()
    for needle, kind in _SUBSTRING_KINDS:
        if needle in low:
            return kind
    return None


def _headers(exc: BaseException) -> dict[str, str]:
    raw = getattr(getattr(exc, "response", None), "headers", None) or getattr(exc, "headers", None)
    if raw is None:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in dict(raw).items()}
    except Exception:
        return {}


def _parse_reset(value: str, *, now: float) -> float | None:
    text = value.strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        number = float(text)
        # Epoch seconds when it is far in the future; otherwise a delay.
        return number - now if number > now - 1 and number > 10**9 else number
    match = _DURATION_RE.fullmatch(text)
    if match and any(match.groups()):
        hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.timestamp() - now
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(text).timestamp() - now
    except (TypeError, ValueError):
        return None


def retry_after_seconds(exc: BaseException, *, now: float | None = None) -> float | None:
    """Seconds until the provider says to retry, from any reset header it sent; `None` when absent."""
    now = time.time() if now is None else now
    headers = _headers(exc)
    delays = [d for name in _RESET_HEADERS if (value := headers.get(name)) is not None and (d := _parse_reset(value, now=now)) is not None]
    if not delays:
        return None
    return min(max(0.0, max(delays)), _MAX_PARK_SECONDS)


@dataclass
class FailoverState:
    """What the middleware is doing right now (shared with the TUI / daemon status)."""

    primary: str = "the primary model"
    active_spec: str | None = None
    parked_until: float | None = None
    parked_kind: str | None = None
    hops: int = 0

    def parked(self, now: float) -> bool:
        """Whether the primary is still parked at `now`."""
        return self.parked_until is not None and now < self.parked_until

    def describe(self, now: float | None = None) -> str:
        """One status line."""
        now = time.time() if now is None else now
        if not self.parked(now) or self.active_spec is None:
            return f"{self.primary} in use"
        clock = time.strftime("%H:%M", time.localtime(self.parked_until or now))
        return f"{self.primary} parked ({self.parked_kind or 'rate_limit'}) until {clock}, answering with {self.active_spec}"


class ProviderFailoverMiddleware(AgentMiddleware[Any, Any, Any]):
    """Rotate through fallback models on rate-limit / quota failures and park the primary.

    Args:
        fallbacks: Ordered `provider:model` specs to try when the primary fails.
        build_model: Builds a chat model from a spec (`None` when it cannot).
        cooldown_seconds: How long to park the primary when the provider sent no
            reset header.
        announce: Prepend a one-line note to the first answer after a hop.
        primary_label: How the primary is named in that note and in `describe()`.
        classify: Failure classifier (`classify_failure` by default).
        clock: Time source (injected for tests).
        on_change: Called with the state after every park / hop / recovery.
    """

    def __init__(
        self,
        fallbacks: Sequence[str],
        *,
        build_model: Callable[[str], BaseChatModel | None],
        cooldown_seconds: float = 300.0,
        announce: bool = True,
        primary_label: str = "the primary model",
        classify: Callable[[BaseException], FailureKind | None] = classify_failure,
        clock: Callable[[], float] = time.time,
        on_change: Callable[[FailoverState], None] | None = None,
    ) -> None:
        """See the class docstring."""
        super().__init__()
        self._fallbacks = [s for s in fallbacks if s]
        self._build_model = build_model
        self._cooldown = max(1.0, float(cooldown_seconds))
        self._announce = announce
        self._classify = classify
        self._clock = clock
        self._on_change = on_change
        self._alts: dict[str, BaseChatModel] = {}
        self.state = FailoverState(primary=primary_label)

    # -- shared -------------------------------------------------------------

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self.state)
            except Exception:
                logger.debug("failover on_change hook failed", exc_info=True)

    def _prepare(self, request: ModelRequest) -> ModelRequest:
        """Route to the sticky alternate while parked; try the primary again once the park expires."""
        now = self._clock()
        if self.state.active_spec is not None:
            if self.state.parked(now):
                alt = self._alts.get(self.state.active_spec)
                if alt is not None:
                    return request.override(model=alt)
            else:
                logger.info("failover: park expired, retrying %s", self.state.primary)
                self.state = FailoverState(primary=self.state.primary, hops=self.state.hops)
                self._changed()
        return request

    def _park(self, exc: BaseException, kind: FailureKind) -> None:
        now = self._clock()
        delay = retry_after_seconds(exc, now=now)
        self.state.parked_until = now + (delay if delay is not None and delay > 0 else self._cooldown)
        self.state.parked_kind = kind

    def _alternate(self, spec: str) -> BaseChatModel | None:
        alt = self._alts.get(spec)
        if alt is None:
            try:
                alt = self._build_model(spec)
            except Exception:
                logger.info("failover: could not build %s", spec, exc_info=True)
                alt = None
            if alt is not None:
                self._alts[spec] = alt
        return alt

    def _candidates(self) -> list[str]:
        return [s for s in self._fallbacks if s != self.state.active_spec]

    def _adopt(self, spec: str, resp: ModelResponse) -> ModelResponse:
        self.state.active_spec = spec
        self.state.hops += 1
        logger.info("failover: %s -> %s (%s)", self.state.primary, spec, self.state.parked_kind)
        self._changed()
        if not self._announce:
            return resp
        note = f"[failover] {self.state.describe(self._clock())}."
        return _prepend_note(resp, note)

    # -- sync ---------------------------------------------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,
    ) -> ModelResponse:
        """Call the model; on a rate-limit / quota failure hop through the fallbacks.

        Raises:
            KeyboardInterrupt: Propagated unchanged.
            SystemExit: Propagated unchanged.
        """
        request = self._prepare(request)
        try:
            return call_next(request)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            kind = self._classify(exc)
            if kind is None or not self._fallbacks:
                raise
            self._park(exc, kind)
            for spec in self._candidates():
                alt = self._alternate(spec)
                if alt is None:
                    continue
                try:
                    resp = call_next(request.override(model=alt))
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as alt_exc:
                    logger.info("failover: %s also failed: %s", spec, alt_exc)
                    continue
                return self._adopt(spec, resp)
            raise

    # -- async --------------------------------------------------------------

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,
    ) -> ModelResponse:
        """Async twin of `wrap_model_call`.

        Raises:
            KeyboardInterrupt: Propagated unchanged.
            SystemExit: Propagated unchanged.
            asyncio.CancelledError: Propagated unchanged.
        """
        request = self._prepare(request)
        try:
            return await call_next(request)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:
            kind = self._classify(exc)
            if kind is None or not self._fallbacks:
                raise
            self._park(exc, kind)
            for spec in self._candidates():
                alt = self._alternate(spec)
                if alt is None:
                    continue
                try:
                    resp = await call_next(request.override(model=alt))
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as alt_exc:
                    logger.info("failover: %s also failed: %s", spec, alt_exc)
                    continue
                return self._adopt(spec, resp)
            raise


def _prepend_note(resp: ModelResponse, note: str) -> ModelResponse:
    result = list(getattr(resp, "result", None) or [])
    if not result or not isinstance(result[0], AIMessage) or not isinstance(result[0].content, str):
        return resp
    first = result[0]
    result[0] = first.model_copy(update={"content": f"{note}\n\n{first.content}" if first.content else note})
    return ModelResponse(result=result, structured_response=getattr(resp, "structured_response", None))


__all__ = [
    "FailoverState",
    "FailureKind",
    "ProviderFailoverMiddleware",
    "classify_failure",
    "retry_after_seconds",
]
