"""Hardening tests for AdaptiveContextMiddleware (P25).

P25: the middleware previously implemented only a no-op `awrap_model_call`
pass-through; its tier config (`truncate_tool_output`, `summarize_at_pct`) was
dead code and it did nothing at all on sync runs. These tests prove the tier
policy now actually shapes the outgoing request:

* an oversized `ToolMessage` is truncated in the request forwarded to the model
  (sync and async paths);
* a small context passes through untouched (same request object, same content);
* the canonical request is never mutated in place;
* structured (non-string) tool content is left intact;
* a handler/shaping failure fails open (the turn still runs).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from bog_agents.middleware.adaptive_context import AdaptiveContextMiddleware

try:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
except ImportError:  # pragma: no cover - import-path fallback
    from langchain.agents.middleware import ModelRequest, ModelResponse  # type: ignore[no-redef,attr-defined]


class _FakeModel:
    """Minimal BaseChatModel stand-in: identifiable, never actually called."""

    _llm_type = "fake"

    def _get_ls_params(self, **_kwargs: Any) -> dict[str, str]:
        return {"ls_provider": "fake", "ls_model_name": "fake-model"}


def _make_request(messages: list[Any]) -> ModelRequest:
    return ModelRequest(
        model=_FakeModel(),
        messages=messages,
        system_message=SystemMessage(content="base system prompt"),
        tools=[],
        runtime=None,
        state={"messages": list(messages)},
    )


def _capture_handler():
    """A sync handler that records the request it received."""

    def handler(request: ModelRequest) -> ModelResponse:
        handler.last_request = request  # type: ignore[attr-defined]
        return ModelResponse(result=[AIMessage(content="ok")])

    return handler


def _acapture_handler():
    """An async handler that records the request it received."""

    async def handler(request: ModelRequest) -> ModelResponse:
        handler.last_request = request  # type: ignore[attr-defined]
        return ModelResponse(result=[AIMessage(content="ok")])

    return handler


def _tool_msg(seen: ModelRequest) -> ToolMessage:
    return next(m for m in seen.messages if isinstance(m, ToolMessage))


def test_oversized_tool_message_truncated_in_outgoing_request_sync() -> None:
    # SMALL tier: tool_output_max_tokens=2000 -> ~8000 char budget.
    mw = AdaptiveContextMiddleware(context_window=8_000)
    budget_chars = mw.tier_config.tool_output_max_tokens * 4
    big = "x" * (budget_chars + 5_000)
    original = ToolMessage(content=big, tool_call_id="t1")
    request = _make_request([HumanMessage(content="hi"), original])

    handler = _capture_handler()
    mw.wrap_model_call(request, handler)
    seen = handler.last_request  # type: ignore[attr-defined]

    forwarded = _tool_msg(seen)
    assert len(forwarded.content) < len(big)
    assert "truncated" in forwarded.content
    # Canonical request untouched (override produced a NEW request/messages).
    assert _tool_msg(request).content == big
    assert seen is not request


async def test_oversized_tool_message_truncated_in_outgoing_request_async() -> None:
    mw = AdaptiveContextMiddleware(context_window=8_000)
    budget_chars = mw.tier_config.tool_output_max_tokens * 4
    big = "y" * (budget_chars + 5_000)
    request = _make_request([HumanMessage(content="hi"), ToolMessage(content=big, tool_call_id="t1")])

    handler = _acapture_handler()
    await mw.awrap_model_call(request, handler)
    seen = handler.last_request  # type: ignore[attr-defined]

    forwarded = _tool_msg(seen)
    assert len(forwarded.content) < len(big)
    assert "truncated" in forwarded.content


def test_small_context_passes_through_unchanged() -> None:
    mw = AdaptiveContextMiddleware(context_window=200_000)
    msgs = [HumanMessage(content="hi"), ToolMessage(content="short output", tool_call_id="t1")]
    request = _make_request(msgs)

    handler = _capture_handler()
    mw.wrap_model_call(request, handler)
    seen = handler.last_request  # type: ignore[attr-defined]

    # No oversized tool output -> the very same request object is forwarded.
    assert seen is request
    assert _tool_msg(seen).content == "short output"


def test_structured_tool_content_left_intact() -> None:
    mw = AdaptiveContextMiddleware(context_window=8_000)
    budget_chars = mw.tier_config.tool_output_max_tokens * 4
    # List/structured content is not a plain string: must be left untouched.
    blocks = [{"type": "text", "text": "z" * (budget_chars + 5_000)}]
    request = _make_request([ToolMessage(content=blocks, tool_call_id="t1")])

    handler = _capture_handler()
    mw.wrap_model_call(request, handler)
    seen = handler.last_request  # type: ignore[attr-defined]

    assert seen is request
    assert _tool_msg(seen).content == blocks


def test_only_oversized_tool_messages_are_truncated() -> None:
    mw = AdaptiveContextMiddleware(context_window=8_000)
    budget_chars = mw.tier_config.tool_output_max_tokens * 4
    small = ToolMessage(content="fine", tool_call_id="s")
    big = ToolMessage(content="b" * (budget_chars + 9_000), tool_call_id="b")
    request = _make_request([HumanMessage(content="hi"), small, big])

    handler = _capture_handler()
    mw.wrap_model_call(request, handler)
    seen = handler.last_request  # type: ignore[attr-defined]

    by_id = {m.tool_call_id: m for m in seen.messages if isinstance(m, ToolMessage)}
    assert by_id["s"].content == "fine"
    assert "truncated" in by_id["b"].content


def test_shaping_failure_fails_open() -> None:
    mw = AdaptiveContextMiddleware(context_window=8_000)
    request = _make_request([HumanMessage(content="hi")])

    # Force _apply_tier to blow up internally; the turn must still run.
    def boom(_msgs: Any) -> Any:
        raise RuntimeError("kaboom")

    mw.truncate_tool_output = boom  # type: ignore[assignment]

    budget_chars = mw.tier_config.tool_output_max_tokens * 4
    request = _make_request([ToolMessage(content="q" * (budget_chars + 5_000), tool_call_id="t1")])

    handler = _capture_handler()
    resp = mw.wrap_model_call(request, handler)
    seen = handler.last_request  # type: ignore[attr-defined]

    # Fail open: original request forwarded, response still produced.
    assert seen is request
    assert isinstance(resp, ModelResponse)
