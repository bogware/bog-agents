"""Unit tests for `bog_agents._messages_reducer._messages_delta_reducer`."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from bog_agents._messages_reducer import _messages_delta_reducer


def test_dedup_by_id_replaces_in_place() -> None:
    """A write carrying an existing id replaces the message at that slot."""
    state = [HumanMessage(content="hi", id="a"), AIMessage(content="first", id="b")]
    writes = [[AIMessage(content="second", id="b")]]
    result = _messages_delta_reducer(state, writes)
    assert [m.id for m in result] == ["a", "b"]
    assert result[1].content == "second"


def test_append_new_id() -> None:
    """A write with a fresh id is appended after existing state."""
    state = [HumanMessage(content="hi", id="a")]
    writes = [[AIMessage(content="reply", id="b")]]
    result = _messages_delta_reducer(state, writes)
    assert [m.id for m in result] == ["a", "b"]


def test_remove_message_tombstones() -> None:
    """A `RemoveMessage` removes the message with the matching id."""
    state = [
        HumanMessage(content="hi", id="a"),
        AIMessage(content="reply", id="b"),
    ]
    writes = [[RemoveMessage(id="a")]]
    result = _messages_delta_reducer(state, writes)
    assert [m.id for m in result] == ["b"]


def test_remove_message_unknown_id_is_noop() -> None:
    """A `RemoveMessage` for an id not present leaves state unchanged."""
    state = [HumanMessage(content="hi", id="a")]
    writes = [[RemoveMessage(id="missing")]]
    result = _messages_delta_reducer(state, writes)
    assert [m.id for m in result] == ["a"]


def test_remove_all_messages_resets_then_keeps_following() -> None:
    """`REMOVE_ALL_MESSAGES` clears prior state and writes before the sentinel."""
    state = [HumanMessage(content="old", id="a"), AIMessage(content="old2", id="b")]
    writes = [
        [
            AIMessage(content="dropped", id="c"),
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            HumanMessage(content="fresh", id="d"),
        ]
    ]
    result = _messages_delta_reducer(state, writes)
    assert [m.id for m in result] == ["d"]
    assert result[0].content == "fresh"


def test_remove_all_messages_uses_last_sentinel() -> None:
    """When multiple sentinels appear, only writes after the last survive."""
    writes = [
        [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            HumanMessage(content="middle", id="a"),
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            HumanMessage(content="last", id="b"),
        ]
    ]
    result = _messages_delta_reducer([HumanMessage(content="x", id="z")], writes)
    assert [m.id for m in result] == ["b"]


def test_id_none_messages_are_appended() -> None:
    """Messages with `id=None` are appended without dedup."""
    state = [HumanMessage(content="hi", id="a")]
    writes = [[AIMessage(content="anon1"), AIMessage(content="anon2")]]
    result = _messages_delta_reducer(state, writes)
    assert len(result) == 3
    assert result[1].content == "anon1"
    assert result[2].content == "anon2"


def test_raw_string_coercion() -> None:
    """A raw string write is coerced to a `HumanMessage`."""
    result = _messages_delta_reducer(None, [["hello world"]])
    assert len(result) == 1
    assert isinstance(result[0], HumanMessage)
    assert result[0].content == "hello world"


def test_raw_dict_coercion() -> None:
    """A raw dict write is coerced to a typed message."""
    result = _messages_delta_reducer(None, [[{"role": "user", "content": "hey", "id": "a"}]])
    assert len(result) == 1
    assert isinstance(result[0], HumanMessage)
    assert result[0].id == "a"


def test_none_state_on_replay() -> None:
    """A `None` state (replay path) is treated as the empty list."""
    writes = [[HumanMessage(content="hi", id="a")]]
    result = _messages_delta_reducer(None, writes)
    assert [m.id for m in result] == ["a"]


def test_non_list_write_is_single_message() -> None:
    """A non-list write entry is treated as one message, not flattened."""
    writes: list = [HumanMessage(content="single", id="a")]
    result = _messages_delta_reducer(None, writes)
    assert len(result) == 1
    assert result[0].content == "single"


def test_raw_dict_state_slow_path() -> None:
    """A raw dict in state hits the slow coercion path and dedups correctly."""
    state = [{"role": "user", "content": "hi", "id": "a"}]
    writes = [[AIMessage(content="reply", id="b")]]
    result = _messages_delta_reducer(state, writes)  # type: ignore[arg-type]
    assert [m.id for m in result] == ["a", "b"]
    assert isinstance(result[0], HumanMessage)
