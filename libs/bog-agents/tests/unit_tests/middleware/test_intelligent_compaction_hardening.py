"""Hardening tests for IntelligentCompactionMiddleware (S20).

Covers the tool_use/tool_result pairing invariant: ``compress_now`` must never
leave the kept verbatim window starting on a ``ToolMessage`` whose issuing
``AIMessage`` was packed into the old range (which would produce an orphaned
``tool_result`` the provider rejects).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bog_agents.middleware.intelligent_compaction import IntelligentCompactionMiddleware


def _ai_with_tool_call(call_id: str) -> AIMessage:
    """Build an AIMessage that issues a single tool call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "do_thing", "args": {}, "id": call_id, "type": "tool_call"}],
    )


def test_compress_now_does_not_orphan_tool_result_at_boundary() -> None:
    """The kept window must not start on a ToolMessage; the issuing AIMessage is kept too."""
    mw = IntelligentCompactionMiddleware(max_packed_tokens=100)

    # 24 messages: keep_count = max(6, 24//4) = 6, so the window starts at index 18.
    # Arrange so that index 18 is a ToolMessage and its issuing AIMessage is at 17.
    messages: list = []
    for i in range(17):
        messages.append(HumanMessage(content=f"filler {i}"))
    messages.append(_ai_with_tool_call("call-x"))  # index 17 (would be packed)
    messages.append(ToolMessage(content="result", tool_call_id="call-x"))  # index 18 (window start)
    for i in range(5):
        messages.append(HumanMessage(content=f"tail {i}"))  # indices 19..23
    assert len(messages) == 24

    compressed, _event = mw.compress_now(messages)

    # First element is the packed SystemMessage; the second is the first kept message.
    first_kept = compressed[1]
    assert not isinstance(first_kept, ToolMessage), "kept window must not start on an orphaned ToolMessage"
    # The issuing AIMessage with the tool_call must be present in the kept window.
    kept = compressed[1:]
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in kept)


def test_compress_now_retreats_past_consecutive_tool_messages() -> None:
    """Multiple consecutive ToolMessages at the boundary all get pulled into the window."""
    mw = IntelligentCompactionMiddleware(max_packed_tokens=100)

    messages: list = []
    for i in range(16):
        messages.append(HumanMessage(content=f"filler {i}"))
    messages.append(_ai_with_tool_call("call-a"))  # index 16
    messages.append(ToolMessage(content="r1", tool_call_id="call-a"))  # index 17
    messages.append(ToolMessage(content="r2", tool_call_id="call-a"))  # index 18 (window start)
    for i in range(5):
        messages.append(HumanMessage(content=f"tail {i}"))
    assert len(messages) == 24

    compressed, _event = mw.compress_now(messages)
    assert not isinstance(compressed[1], ToolMessage)
    assert isinstance(compressed[1], AIMessage)


def test_compress_now_unchanged_when_boundary_is_not_tool_message() -> None:
    """When the window does not start on a ToolMessage, behaviour is unchanged."""
    mw = IntelligentCompactionMiddleware(max_packed_tokens=100)

    messages: list = [HumanMessage(content=f"msg {i}") for i in range(24)]
    compressed, _event = mw.compress_now(messages)

    # keep_count = 6, so 6 trailing HumanMessages are kept verbatim after the packed block.
    assert len(compressed) == 7
    assert not isinstance(compressed[1], ToolMessage)
