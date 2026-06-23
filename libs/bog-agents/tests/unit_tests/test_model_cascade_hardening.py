"""Hardening tests for ModelCascadeMiddleware routing (P23).

Verifies that the previously pure pass-through `awrap_model_call` now performs
real cost-aware routing: a prompt that should downshift overrides the request
model, while an ambiguous/uncertain prompt passes through unchanged and routing
failures never crash a turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

import bog_agents.middleware.model_cascade as mc
from bog_agents.middleware.model_cascade import ModelCascadeMiddleware


@dataclass
class _FakeRequest:
    """Minimal stand-in for a frozen ModelRequest with an immutable override."""

    messages: list[Any] = field(default_factory=list)
    model: Any = "anthropic:claude-opus-4-6"

    def override(self, **kwargs: Any) -> _FakeRequest:
        return replace(self, **kwargs)


class _Sentinel:
    """A resolved-model sentinel returned by the patched resolve_model."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id


def _patch_resolve(monkeypatch) -> dict[str, str]:
    """Patch resolve_model so no network/socket is touched.

    Returns:
        A dict capturing the last model_id passed to resolve_model.
    """
    captured: dict[str, str] = {}

    def fake_resolve(model: str) -> _Sentinel:
        captured["model_id"] = model
        return _Sentinel(model)

    monkeypatch.setattr(mc, "resolve_model", fake_resolve, raising=False)
    # resolve_model is imported lazily inside _route_request via
    # `from bog_agents._models import resolve_model`; patch the source too.
    import bog_agents._models as models_mod

    monkeypatch.setattr(models_mod, "resolve_model", fake_resolve, raising=False)
    return captured


async def _drive(mw: ModelCascadeMiddleware, request: _FakeRequest) -> _FakeRequest:
    """Run the async hook and return the request actually forwarded downstream."""
    forwarded: dict[str, _FakeRequest] = {}

    async def call_next(req: _FakeRequest) -> str:
        forwarded["req"] = req
        return "ok"

    await mw.awrap_model_call(request, call_next)  # type: ignore[arg-type]
    return forwarded["req"]


def _drive_sync(mw: ModelCascadeMiddleware, request: _FakeRequest) -> _FakeRequest:
    forwarded: dict[str, _FakeRequest] = {}

    def call_next(req: _FakeRequest) -> str:
        forwarded["req"] = req
        return "ok"

    mw.wrap_model_call(request, call_next)  # type: ignore[arg-type]
    return forwarded["req"]


class TestCascadeRoutingDownshift:
    """A trivial prompt must downshift the model to the cheapest tier."""

    async def test_async_trivial_overrides_model(self, monkeypatch):
        captured = _patch_resolve(monkeypatch)
        mw = ModelCascadeMiddleware()
        req = _FakeRequest(messages=[HumanMessage(content="what is the version?")])

        forwarded = await _drive(mw, req)

        # The trivial prompt routes to the "fast" tier and overrides the model.
        assert isinstance(forwarded.model, _Sentinel)
        assert forwarded.model.model_id == "anthropic:claude-haiku-4-5"
        assert captured["model_id"] == "anthropic:claude-haiku-4-5"
        # The cascade actually recorded a routing decision.
        assert mw.history.total_decisions == 1
        assert mw.history.decisions[-1].selected_tier == "fast"

    def test_sync_trivial_overrides_model(self, monkeypatch):
        _patch_resolve(monkeypatch)
        mw = ModelCascadeMiddleware()
        req = _FakeRequest(messages=[HumanMessage(content="show me the status")])

        forwarded = _drive_sync(mw, req)

        assert isinstance(forwarded.model, _Sentinel)
        assert forwarded.model.model_id == "anthropic:claude-haiku-4-5"

    async def test_list_content_human_message_routes(self, monkeypatch):
        _patch_resolve(monkeypatch)
        mw = ModelCascadeMiddleware()
        req = _FakeRequest(
            messages=[HumanMessage(content=[{"type": "text", "text": "list the files"}])],
        )

        forwarded = await _drive(mw, req)

        assert isinstance(forwarded.model, _Sentinel)
        assert forwarded.model.model_id == "anthropic:claude-haiku-4-5"


class TestCascadePassThrough:
    """Uncertain / frontier-tier prompts must pass through unchanged."""

    async def test_expert_prompt_passes_through(self, monkeypatch):
        captured = _patch_resolve(monkeypatch)
        mw = ModelCascadeMiddleware()
        original_model = "anthropic:claude-opus-4-6"
        req = _FakeRequest(
            messages=[HumanMessage(content="design the entire distributed system from scratch")],
            model=original_model,
        )

        forwarded = await _drive(mw, req)

        # Frontier tier chosen -> never override; resolve_model not called.
        assert forwarded.model == original_model
        assert "model_id" not in captured

    async def test_no_human_message_passes_through(self, monkeypatch):
        captured = _patch_resolve(monkeypatch)
        mw = ModelCascadeMiddleware()
        original_model = "anthropic:claude-opus-4-6"
        req = _FakeRequest(messages=[AIMessage(content="just an assistant turn")], model=original_model)

        forwarded = await _drive(mw, req)

        assert forwarded.model == original_model
        assert "model_id" not in captured
        # No routing decision recorded when there is nothing to route on.
        assert mw.history.total_decisions == 0

    async def test_empty_messages_passes_through(self, monkeypatch):
        _patch_resolve(monkeypatch)
        mw = ModelCascadeMiddleware()
        original_model = "anthropic:claude-opus-4-6"
        req = _FakeRequest(messages=[], model=original_model)

        forwarded = await _drive(mw, req)

        assert forwarded.model == original_model


class TestCascadeNeverCrashes:
    """Routing failures must degrade to the original request, never raise."""

    async def test_resolve_failure_falls_through(self, monkeypatch):
        import bog_agents._models as models_mod

        def boom(model: str) -> Any:
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(models_mod, "resolve_model", boom, raising=False)
        monkeypatch.setattr(mc, "resolve_model", boom, raising=False)

        mw = ModelCascadeMiddleware()
        original_model = "anthropic:claude-opus-4-6"
        # Trivial prompt would normally downshift and call resolve_model.
        req = _FakeRequest(messages=[HumanMessage(content="what is the version?")], model=original_model)

        forwarded = await _drive(mw, req)

        # resolve_model raised -> fall through unchanged, no exception bubbled.
        assert forwarded.model == original_model
