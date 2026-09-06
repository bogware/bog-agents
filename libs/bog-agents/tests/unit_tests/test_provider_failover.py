"""ROADMAP #53: provider-agnostic rate-limit failover."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from bog_agents.middleware.provider_failover import (
    FailoverState,
    ProviderFailoverMiddleware,
    classify_failure,
    retry_after_seconds,
)


class _Status(Exception):
    def __init__(self, code: int, message: str = "", headers: dict[str, str] | None = None) -> None:
        super().__init__(message or f"status {code}")
        self.status_code = code
        self.headers = headers or {}


class RateLimitError(Exception):
    pass


class _Model:
    def __init__(self, name: str) -> None:
        self.name = name


class _Request:
    def __init__(self, model: object) -> None:
        self.model = model

    def override(self, *, model: object = None, **_kw: object) -> _Request:
        return _Request(model if model is not None else self.model)


class TestClassify:
    def test_status_codes_type_names_and_substrings(self) -> None:
        assert classify_failure(_Status(429)) == "rate_limit"
        assert classify_failure(_Status(529)) == "overloaded"
        assert classify_failure(_Status(503)) == "unavailable"
        assert classify_failure(RateLimitError("nope")) == "rate_limit"
        assert classify_failure(RuntimeError("Error: overloaded_error")) == "overloaded"
        assert classify_failure(RuntimeError("You exceeded your current quota")) == "quota"
        assert classify_failure(ValueError("boom")) is None
        assert classify_failure(_Status(500, "internal")) is None

    def test_retry_after_from_headers(self) -> None:
        now = 1_800_000_000.0
        assert retry_after_seconds(_Status(429, headers={"Retry-After": "7"}), now=now) == 7.0
        assert retry_after_seconds(_Status(429, headers={"x-ratelimit-reset-requests": "6m0s"}), now=now) == 360.0
        assert retry_after_seconds(_Status(429, headers={"x-ratelimit-reset-tokens": "1h2m3s"}), now=now) == 3723.0
        iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 90))
        assert retry_after_seconds(_Status(429, headers={"anthropic-ratelimit-requests-reset": iso}), now=now) == pytest.approx(90.0)
        assert retry_after_seconds(_Status(429, headers={"x-ratelimit-reset": str(int(now + 45))}), now=now) == pytest.approx(45.0)
        assert retry_after_seconds(_Status(429), now=now) is None
        assert retry_after_seconds(_Status(429, headers={"retry-after": "999999"}), now=now) == 6 * 3600.0


class TestMiddleware:
    def _build(
        self, clock: list[float], *, cooldown: float = 300.0, announce: bool = True
    ) -> tuple[ProviderFailoverMiddleware, list[str], dict[str, bool]]:
        seen: list[str] = []
        failing = {"primary": True, "ollama:a": True, "ollama:b": False}
        mw = ProviderFailoverMiddleware(
            ["ollama:a", "ollama:b"],
            build_model=_Model,
            cooldown_seconds=cooldown,
            announce=announce,
            primary_label="anthropic:claude",
            clock=lambda: clock[0],
        )

        def call_next(req: _Request) -> ModelResponse:
            name = req.model.name  # type: ignore[attr-defined]
            seen.append(name)
            if failing[name]:
                raise _Status(429, "rate limited")
            return ModelResponse(result=[AIMessage(content=f"hi from {name}")])

        mw._call_next = call_next  # type: ignore[attr-defined]
        return mw, seen, failing

    def test_hops_to_the_first_working_fallback_and_sticks(self) -> None:
        clock = [1000.0]
        mw, seen, failing = self._build(clock)
        call_next = mw._call_next  # type: ignore[attr-defined]

        resp = mw.wrap_model_call(request=_Request(_Model("primary")), call_next=call_next)
        content = resp.result[0].content
        assert "hi from ollama:b" in content and "[failover]" in content and "anthropic:claude parked" in content
        assert seen == ["primary", "ollama:a", "ollama:b"]
        assert mw.state.active_spec == "ollama:b" and mw.state.hops == 1
        assert mw.state.parked_until == pytest.approx(1300.0)
        assert "answering with ollama:b" in mw.state.describe(clock[0])

        # Still parked: the next call goes straight to the alternate, no note.
        seen.clear()
        resp = mw.wrap_model_call(request=_Request(_Model("primary")), call_next=call_next)
        assert seen == ["ollama:b"] and resp.result[0].content == "hi from ollama:b"

        # Park expired and the primary recovered: state resets.
        clock[0] = 1301.0
        failing["primary"] = False
        seen.clear()
        resp = mw.wrap_model_call(request=_Request(_Model("primary")), call_next=call_next)
        assert seen == ["primary"] and resp.result[0].content == "hi from primary"
        assert mw.state.active_spec is None and mw.state.describe(clock[0]) == "anthropic:claude in use"

    def test_reset_header_sets_the_park_and_unrelated_errors_propagate(self) -> None:
        clock = [1000.0]
        mw, _seen, _failing = self._build(clock)

        def call_next(req: _Request) -> ModelResponse:
            name = req.model.name  # type: ignore[attr-defined]
            if name == "primary":
                raise _Status(429, headers={"retry-after": "42"})
            return ModelResponse(result=[AIMessage(content=name)])

        mw.wrap_model_call(request=_Request(_Model("primary")), call_next=call_next)
        assert mw.state.parked_until == pytest.approx(1042.0)

        fresh, _seen, _failing = self._build(clock)
        with pytest.raises(ValueError, match="bug"):
            fresh.wrap_model_call(request=_Request(_Model("primary")), call_next=lambda req: (_ for _ in ()).throw(ValueError("bug")))
        assert fresh.state.active_spec is None

    def test_all_fallbacks_failing_raises_the_original(self) -> None:
        clock = [1000.0]
        mw, seen, failing = self._build(clock)
        failing["ollama:b"] = True
        with pytest.raises(_Status):
            mw.wrap_model_call(request=_Request(_Model("primary")), call_next=mw._call_next)  # type: ignore[attr-defined]
        assert seen == ["primary", "ollama:a", "ollama:b"]
        assert mw.state.active_spec is None and mw.state.parked_until is not None

    def test_async_path_and_on_change(self) -> None:
        clock = [1000.0]
        states: list[str] = []
        mw = ProviderFailoverMiddleware(
            ["ollama:b"],
            build_model=_Model,
            clock=lambda: clock[0],
            announce=False,
            on_change=lambda s: states.append(s.active_spec or "-"),
        )

        async def call_next(req: _Request) -> ModelResponse:
            name = req.model.name  # type: ignore[attr-defined]
            if name == "primary":
                raise RateLimitError("slow down")
            return ModelResponse(result=[AIMessage(content=name)])

        resp = asyncio.run(mw.awrap_model_call(request=_Request(_Model("primary")), call_next=call_next))
        assert resp.result[0].content == "ollama:b"  # announce=False: untouched
        assert states == ["ollama:b"]

    def test_unbuildable_fallbacks_are_skipped(self) -> None:
        built: list[str] = []

        def build(spec: str) -> Any:
            built.append(spec)
            return None if spec == "broken" else _Model(spec)

        mw = ProviderFailoverMiddleware(["broken", "ollama:b"], build_model=build, clock=lambda: 5.0)

        def call_next(req: _Request) -> ModelResponse:
            name = req.model.name  # type: ignore[attr-defined]
            if name == "primary":
                raise _Status(429)
            return ModelResponse(result=[AIMessage(content=name)])

        resp = mw.wrap_model_call(request=_Request(_Model("primary")), call_next=call_next)
        assert "ollama:b" in resp.result[0].content and built == ["broken", "ollama:b"]

    def test_state_describe_without_park(self) -> None:
        assert FailoverState(primary="x").describe(0.0) == "x in use"
