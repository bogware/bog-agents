"""Unit tests for `OutputTruncationMiddleware` auto-continuation."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from bog_agents.middleware.output_truncation import OutputTruncationMiddleware

AnyModelResponse = ModelResponse


def _make_request() -> ModelRequest[Any]:
    return ModelRequest[Any](
        model=MagicMock(),
        messages=[HumanMessage(content="Write a long essay")],
    )


def _aim(
    content: str | list,
    *,
    truncated: bool = False,
    tool_calls: list | None = None,
    msg_id: str | None = None,
) -> AIMessage:
    finish_reason = "length" if truncated else "stop"
    return AIMessage(
        content=content,
        response_metadata={"finish_reason": finish_reason},
        tool_calls=tool_calls or [],
        id=msg_id,
    )


def _calls(handler: MagicMock) -> list[ModelRequest[Any]]:
    return [call.args[0] for call in handler.call_args_list]


def test_untruncated_response_passes_through() -> None:
    middleware = OutputTruncationMiddleware()
    handler = MagicMock(return_value=_aim("Complete answer"))
    result = middleware.wrap_model_call(_make_request(), handler)

    assert handler.call_count == 1
    assert isinstance(result, AnyModelResponse)
    assert result.result[0].content == "Complete answer"


def test_truncated_response_is_continued_and_merged() -> None:
    middleware = OutputTruncationMiddleware()
    first = _aim("Part one of the answer", truncated=True, msg_id="msg-1")
    second = _aim("Part two finishes it.")
    handler = MagicMock(side_effect=[first, second])

    result = middleware.wrap_model_call(_make_request(), handler)

    assert handler.call_count == 2
    merged = result.result[0]
    assert isinstance(merged, AIMessage)
    assert merged.content == "Part one of the answer\nPart two finishes it."
    # Partial message id is preserved so the framework sees one logical message.
    assert merged.id == "msg-1"
    # Terminal (non-truncated) metadata is used.
    assert merged.response_metadata["finish_reason"] == "stop"


def test_continuation_receives_partial_plus_instruction() -> None:
    middleware = OutputTruncationMiddleware()
    first = _aim("Part one", truncated=True)
    handler = MagicMock(side_effect=[first, _aim("Part two")])

    middleware.wrap_model_call(_make_request(), handler)

    requests = _calls(handler)
    assert len(requests) == 2
    # Continuation appends the partial message and a continue instruction.
    assert requests[1].messages[-2:] == [
        first,
        HumanMessage(content=middleware._continue_prompt),
    ]
    # The continue instruction is configurable.
    assert "output token limit" in middleware._continue_prompt


def test_max_continues_exhausted_returns_accumulated_text() -> None:
    middleware = OutputTruncationMiddleware(max_continues=2)
    handler = MagicMock(side_effect=[_aim("A", truncated=True), _aim("B", truncated=True), _aim("C", truncated=True)])

    result = middleware.wrap_model_call(_make_request(), handler)

    # 1 initial call + 2 continuations (budget exhausted).
    assert handler.call_count == 3
    assert isinstance(result, AnyModelResponse)
    assert result.result[0].content == "A\nB\nC"
    # Still flagged as truncated so the caller knows the text is incomplete.
    assert result.result[0].response_metadata["finish_reason"] == "length"


def test_tool_call_response_is_not_healed() -> None:
    middleware = OutputTruncationMiddleware()
    tool_call = {"name": "some_tool", "args": {"q": 1}, "id": "call_1", "type": "tool_call"}
    handler = MagicMock(return_value=_aim("", tool_calls=[tool_call], truncated=True))

    result = middleware.wrap_model_call(_make_request(), handler)

    assert handler.call_count == 1
    assert isinstance(result, AnyModelResponse)
    assert result.result[0].tool_calls


def test_structured_output_request_is_not_healed() -> None:
    middleware = OutputTruncationMiddleware()
    request = _make_request().override(response_format=MagicMock())
    handler = MagicMock(return_value=_aim("Part one", truncated=True))

    middleware.wrap_model_call(request, handler)

    assert handler.call_count == 1


def test_content_blocks_text_is_healed() -> None:
    middleware = OutputTruncationMiddleware()
    blocks = [{"type": "text", "text": "Block one"}]
    handler = MagicMock(side_effect=[_aim(blocks, truncated=True), _aim("Block two")])

    result = middleware.wrap_model_call(_make_request(), handler)

    assert handler.call_count == 2
    assert isinstance(result, AnyModelResponse)
    assert result.result[0].content == "Block one\nBlock two"


@pytest.mark.anyio
async def test_async_truncated_response_is_healed() -> None:
    middleware = OutputTruncationMiddleware()
    handler = MagicMock(side_effect=[_aim("Async part one", truncated=True), _aim("Async part two")])

    async def ahandler(req: ModelRequest[Any]) -> AnyModelResponse:
        return handler(req)

    result = await middleware.awrap_model_call(_make_request(), ahandler)

    assert handler.call_count == 2
    assert isinstance(result, AnyModelResponse)
    assert result.result[0].content == "Async part one\nAsync part two"


def test_constructor_validates_max_continues() -> None:
    with pytest.raises(ValueError, match="max_continues"):
        OutputTruncationMiddleware(max_continues=0)


def test_implements_agent_middleware_protocol() -> None:
    # Ensure the middleware is a proper AgentMiddleware subclass so the SDK
    # can compose it in graph.py defaults.
    assert isinstance(OutputTruncationMiddleware(), AgentMiddleware)


def test_not_truncated_after_non_truncation_reason() -> None:
    middleware = OutputTruncationMiddleware()
    # finish_reason="stop" even with an empty-ish text must not heal.
    handler = MagicMock(return_value=_aim("Done", truncated=False))
    result = middleware.wrap_model_call(_make_request(), handler)
    assert handler.call_count == 1
    assert isinstance(result, AnyModelResponse)
    assert result.result[0].content == "Done"
