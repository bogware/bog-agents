"""Hardening tests for DLPMiddleware redact mode (P14).

P14: in ``redact`` mode the middleware previously rewrote ``msg.content`` and
multimodal ``part["text"]`` *in place* on the shared LangGraph state message
objects, permanently corrupting the canonical history for all later turns,
checkpoints, and summarization (violating the immutability invariant the
StreetSweeper preserves).

These tests prove the new behavior:

* the outgoing model call sees redacted content (DLP still works);
* the original messages in state are left untouched (string content);
* the original multimodal part dicts are left untouched (no in-place mutation);
* warn mode forwards the request unchanged;
* a no-match call returns the very same request object (no needless copy).
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bog_agents.middleware.dlp import DLPMiddleware

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


# ---------------------------------------------------------------------------
# String content: model sees redaction, canonical state preserved
# ---------------------------------------------------------------------------


def test_redact_model_sees_redaction_state_preserved_sync() -> None:
    mw = DLPMiddleware(mode="redact")
    secret = "My SSN is 123-45-6789 please help"
    msg = HumanMessage(content=secret)
    request = _make_request([msg])
    handler = _capture_handler()

    mw.wrap_model_call(request, handler)

    # The model saw the redacted view.
    forwarded = handler.last_request  # type: ignore[attr-defined]
    assert "[SSN-REDACTED]" in forwarded.messages[0].content
    assert "123-45-6789" not in forwarded.messages[0].content

    # The canonical message object in state is untouched.
    assert msg.content == secret
    assert request.messages[0] is msg
    assert request.messages[0].content == secret


async def test_redact_model_sees_redaction_state_preserved_async() -> None:
    mw = DLPMiddleware(mode="redact")
    secret = "Card 4111 1111 1111 1111 on file"
    msg = HumanMessage(content=secret)
    request = _make_request([msg])
    handler = _acapture_handler()

    await mw.awrap_model_call(request, handler)

    forwarded = handler.last_request  # type: ignore[attr-defined]
    assert "[CC-REDACTED]" in forwarded.messages[0].content
    assert "4111 1111 1111 1111" not in forwarded.messages[0].content

    # Original preserved.
    assert msg.content == secret


def test_redacted_request_is_a_new_object_not_in_place() -> None:
    mw = DLPMiddleware(mode="redact")
    msg = HumanMessage(content="ssn 123-45-6789")
    request = _make_request([msg])
    handler = _capture_handler()

    mw.wrap_model_call(request, handler)
    forwarded = handler.last_request  # type: ignore[attr-defined]

    # A distinct copy reached the model; the original message identity is intact.
    assert forwarded.messages[0] is not msg
    assert request.messages[0] is msg


# ---------------------------------------------------------------------------
# Multimodal (list) content: shared part dicts are never mutated
# ---------------------------------------------------------------------------


def test_redact_multimodal_part_dicts_not_mutated() -> None:
    mw = DLPMiddleware(mode="redact")
    part = {"type": "text", "text": "contact me at a@b.com"}
    other = {"type": "image_url", "image_url": {"url": "http://x/y.png"}}
    content = [part, other]
    msg = HumanMessage(content=content)
    request = _make_request([msg])
    handler = _capture_handler()

    mw.wrap_model_call(request, handler)
    forwarded = handler.last_request  # type: ignore[attr-defined]

    # Model saw redacted text.
    fwd_content = forwarded.messages[0].content
    assert fwd_content[0]["text"] == "contact me at [EMAIL-REDACTED]"

    # The original message's content list is untouched (Pydantic validates the
    # list into the model on construction, so identity differs from the literal
    # we passed in; what matters is the canonical text was NOT redacted).
    assert part["text"] == "contact me at a@b.com"
    assert msg.content[0]["text"] == "contact me at a@b.com"
    # The forwarded copy is a distinct list object from canonical state.
    assert forwarded.messages[0].content is not msg.content
    assert forwarded.messages[0].content[1] == msg.content[1]


# ---------------------------------------------------------------------------
# warn mode + no-match: no copy, same request object forwarded
# ---------------------------------------------------------------------------


def test_warn_mode_forwards_original_request() -> None:
    mw = DLPMiddleware(mode="warn")
    secret = "ssn 123-45-6789"
    msg = HumanMessage(content=secret)
    request = _make_request([msg])
    handler = _capture_handler()

    mw.wrap_model_call(request, handler)
    forwarded = handler.last_request  # type: ignore[attr-defined]

    # warn never rewrites: same request object, original content intact.
    assert forwarded is request
    assert request.messages[0].content == secret
    # Still recorded a detection.
    assert mw.log.total_detections == 1
    assert mw.log.total_redactions == 0


def test_no_match_returns_same_request_object() -> None:
    mw = DLPMiddleware(mode="redact")
    msg = HumanMessage(content="nothing sensitive here")
    request = _make_request([msg])
    handler = _capture_handler()

    mw.wrap_model_call(request, handler)
    forwarded = handler.last_request  # type: ignore[attr-defined]

    assert forwarded is request
    assert mw.log.total_detections == 0
