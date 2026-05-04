"""Tests for RemoteAgent, _convert_message_data, and helpers."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from bog_agents_cli.remote_client import (
    RemoteAgent,
    _convert_ai_message,
    _convert_human_message,
    _convert_interrupts,
    _convert_message_data,
    _convert_tool_message,
    _prepare_config,
)

_TEST_THREAD_ID = "01966f3a-0000-7000-8000-000000000001"


# ---------------------------------------------------------------------------
# _prepare_config
# ---------------------------------------------------------------------------


class TestPrepareConfig:
    def test_preserves_thread_id(self) -> None:
        config = {"configurable": {"thread_id": _TEST_THREAD_ID}}
        result = _prepare_config(config)
        assert result["configurable"]["thread_id"] == _TEST_THREAD_ID

    def test_none_config(self) -> None:
        result = _prepare_config(None)
        assert result == {"configurable": {}}

    def test_does_not_mutate_original(self) -> None:
        tid = str(uuid.uuid4())
        config = {"configurable": {"thread_id": tid}}
        _prepare_config(config)
        assert config["configurable"]["thread_id"] == tid

    def test_missing_configurable_key(self) -> None:
        result = _prepare_config({"other": "value"})
        assert result["configurable"] == {}

    def test_empty_string_thread_id_not_converted(self) -> None:
        result = _prepare_config({"configurable": {"thread_id": ""}})
        assert result["configurable"]["thread_id"] == ""


# ---------------------------------------------------------------------------
# _convert_message_data
# ---------------------------------------------------------------------------


class TestConvertMessageData:
    def test_ai_message_text(self) -> None:
        msg = _convert_message_data({"type": "ai", "content": "Hello", "id": "m1"})
        assert isinstance(msg, AIMessageChunk)
        assert msg.content == "Hello"
        assert msg.id == "m1"

    def test_ai_message_with_tool_call_chunks(self) -> None:
        msg = _convert_message_data(
            {
                "type": "AIMessageChunk",
                "content": "",
                "id": "m1",
                "tool_call_chunks": [
                    {"name": "search", "args": '{"q":', "id": "tc1", "index": 0}
                ],
            }
        )
        assert isinstance(msg, AIMessageChunk)
        tc_blocks = [
            b for b in msg.content_blocks if b.get("type") == "tool_call_chunk"
        ]
        assert len(tc_blocks) == 1
        assert tc_blocks[0]["name"] == "search"
        assert tc_blocks[0]["args"] == '{"q":'

    def test_ai_message_with_string_args_tool_calls(self) -> None:
        msg = _convert_message_data(
            {
                "type": "ai",
                "content": "",
                "id": "m1",
                "tool_calls": [{"name": "ls", "args": '{"path":"/"', "id": "tc1"}],
            }
        )
        assert isinstance(msg, AIMessageChunk)
        tc_blocks = [
            b for b in msg.content_blocks if b.get("type") == "tool_call_chunk"
        ]
        assert len(tc_blocks) == 1

    def test_ai_message_with_dict_args_tool_calls(self) -> None:
        msg = _convert_message_data(
            {
                "type": "ai",
                "content": "",
                "id": "m1",
                "tool_calls": [{"name": "search", "args": {"q": "test"}, "id": "tc1"}],
            }
        )
        assert isinstance(msg, AIMessageChunk)
        assert msg.tool_calls[0]["name"] == "search"

    def test_ai_message_usage_metadata(self) -> None:
        msg = _convert_message_data(
            {
                "type": "ai",
                "content": "",
                "id": "m1",
                "usage_metadata": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "total_tokens": 30,
                },
            }
        )
        assert msg.usage_metadata["input_tokens"] == 10

    def test_ai_message_type_alias(self) -> None:
        msg = _convert_message_data({"type": "AIMessage", "content": "Hi", "id": "m1"})
        assert isinstance(msg, AIMessageChunk)
        assert msg.content == "Hi"

    def test_human_message(self) -> None:
        msg = _convert_message_data({"type": "human", "content": "Hi", "id": "m1"})
        assert isinstance(msg, HumanMessage)
        assert msg.content == "Hi"

    def test_human_message_type_alias(self) -> None:
        msg = _convert_message_data(
            {"type": "HumanMessage", "content": "Hey", "id": "m1"}
        )
        assert isinstance(msg, HumanMessage)
        assert msg.content == "Hey"

    def test_tool_message(self) -> None:
        msg = _convert_message_data(
            {
                "type": "tool",
                "content": "Sunny",
                "tool_call_id": "tc1",
                "name": "weather",
                "id": "m2",
            }
        )
        assert isinstance(msg, ToolMessage)
        assert msg.content == "Sunny"
        assert msg.tool_call_id == "tc1"

    def test_tool_message_type_alias(self) -> None:
        msg = _convert_message_data(
            {
                "type": "ToolMessage",
                "content": "result",
                "tool_call_id": "tc1",
                "name": "search",
                "id": "m3",
            }
        )
        assert isinstance(msg, ToolMessage)
        assert msg.content == "result"

    def test_tool_message_defaults(self) -> None:
        msg = _convert_message_data({"type": "tool", "id": "m1"})
        assert isinstance(msg, ToolMessage)
        assert msg.content == ""
        assert msg.tool_call_id == ""
        assert msg.name == ""
        assert msg.status == "success"

    def test_unknown_type_returns_none(self) -> None:
        assert _convert_message_data({"type": "unknown"}) is None


# ---------------------------------------------------------------------------
# _convert_interrupts
# ---------------------------------------------------------------------------


class TestConvertInterrupts:
    def test_dicts_to_interrupt_objects(self) -> None:
        from langgraph.types import Interrupt

        result = _convert_interrupts([{"value": {"type": "ask_user"}, "id": "int-1"}])
        assert len(result) == 1
        assert isinstance(result[0], Interrupt)
        assert result[0].value == {"type": "ask_user"}
        assert result[0].id == "int-1"

    def test_interrupt_objects_passed_through(self) -> None:
        from langgraph.types import Interrupt

        obj = Interrupt(value="test", id="int-2")
        result = _convert_interrupts([obj])
        assert result[0] is obj

    def test_non_list_wraps_value(self) -> None:
        assert _convert_interrupts("not a list") == ["not a list"]

    def test_none_returns_empty(self) -> None:
        assert _convert_interrupts(None) == []

    def test_dict_without_value_passed_through(self) -> None:
        raw = [{"id": "x", "other": 123}]
        result = _convert_interrupts(raw)
        assert result[0] == {"id": "x", "other": 123}

    def test_interrupt_dict_missing_id_defaults_to_empty(self) -> None:
        from langgraph.types import Interrupt

        result = _convert_interrupts([{"value": "confirm"}])
        assert isinstance(result[0], Interrupt)
        assert result[0].value == "confirm"
        assert result[0].id == ""


# ---------------------------------------------------------------------------
# Helpers for RemoteAgent tests
# ---------------------------------------------------------------------------


def _make_agent(
    events: list[tuple[tuple[str, ...], str, Any]],
) -> RemoteAgent:
    """Create a RemoteAgent with a mock RemoteGraph yielding events."""
    agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
    mock_graph = MagicMock()

    async def fake_astream(
        input: Any,  # noqa: A002, ANN401
        **kwargs: Any,
    ) -> Any:  # noqa: ANN401
        for ev in events:
            yield ev

    mock_graph.astream = fake_astream
    agent._graph = mock_graph
    return agent


def _config() -> dict[str, Any]:
    return {"configurable": {"thread_id": _TEST_THREAD_ID}}


# ---------------------------------------------------------------------------
# RemoteAgent — astream delegation
# ---------------------------------------------------------------------------


class TestRemoteAgentAstream:
    async def test_text_message_converted(self) -> None:
        """Messages-tuple text chunks are converted to AIMessageChunk."""
        events = [((), "messages", ({"type": "ai", "content": "Hi", "id": "m1"}, {}))]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        ns, mode, (msg, _meta) = results[0]
        assert ns == ()
        assert mode == "messages"
        assert isinstance(msg, AIMessageChunk)
        assert msg.content == "Hi"

    async def test_tool_message_converted(self) -> None:
        """Tool messages are converted to ToolMessage."""
        events = [
            (
                (),
                "messages",
                (
                    {
                        "type": "tool",
                        "content": "Sunny",
                        "tool_call_id": "tc1",
                        "name": "weather",
                        "id": "m2",
                    },
                    {},
                ),
            )
        ]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        assert isinstance(results[0][2][0], ToolMessage)

    async def test_updates_with_interrupt_converted(self) -> None:
        """Interrupt dicts in updates events are converted to Interrupt."""
        from langgraph.types import Interrupt

        events = [
            (
                (),
                "updates",
                {"__interrupt__": [{"value": {"type": "ask_user"}, "id": "int-1"}]},
            )
        ]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        interrupts = results[0][2]["__interrupt__"]
        assert isinstance(interrupts[0], Interrupt)

    async def test_updates_without_interrupt_passed_through(self) -> None:
        """Regular updates events pass through unchanged."""
        events = [((), "updates", {"agent": {"messages": []}})]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        assert results[0][1] == "updates"
        assert results[0][2] == {"agent": {"messages": []}}

    async def test_namespace_preserved(self) -> None:
        """Namespace from RemoteGraph is preserved in output."""
        events = [
            (
                ("sub", "inner"),
                "messages",
                ({"type": "ai", "content": "Hi", "id": "m1"}, {}),
            )
        ]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert results[0][0] == ("sub", "inner")

    async def test_unknown_message_type_skipped(self) -> None:
        """Unknown message types don't produce output."""
        events = [((), "messages", ({"type": "unknown", "content": "?"}, {}))]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert results == []

    async def test_missing_thread_id_raises(self) -> None:
        """Raises ValueError if thread_id is missing."""
        agent = _make_agent([])
        with pytest.raises(ValueError, match="thread_id"):
            async for _ in agent.astream({"messages": []}, config={"configurable": {}}):
                pass

    async def test_rapid_streaming(self) -> None:
        """Many rapid text events all arrive (no dropped tokens)."""
        events = [
            (
                (),
                "messages",
                ({"type": "ai", "content": f"tok{i}", "id": "m1"}, {}),
            )
            for i in range(100)
        ]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        combined = "".join(r[2][0].content for r in results)
        assert combined == "".join(f"tok{i}" for i in range(100))
        assert len(results) == 100

    async def test_non_dict_message_object_passed_through(self) -> None:
        """Pre-deserialized LangChain message objects are yielded as-is."""
        chunk = AIMessageChunk(content="pre-built", id="m1")
        events = [((), "messages", (chunk, {"run_id": "r1"}))]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        assert results[0][2][0] is chunk
        assert results[0][2][1] == {"run_id": "r1"}

    async def test_meta_none_defaults_to_empty_dict(self) -> None:
        """None metadata is normalized to empty dict."""
        events = [((), "messages", ({"type": "ai", "content": "x", "id": "m1"}, None))]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert results[0][2][1] == {}

    async def test_unknown_mode_passed_through(self) -> None:
        """Events with unknown modes are yielded unchanged."""
        events = [((), "values", {"key": "val"})]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        assert results[0] == ((), "values", {"key": "val"})

    async def test_non_dict_updates_falls_through(self) -> None:
        """Non-dict updates data passes through the generic yield."""
        events = [((), "updates", "string_data")]
        agent = _make_agent(events)
        results = [
            item async for item in agent.astream({"messages": []}, config=_config())
        ]
        assert len(results) == 1
        assert results[0] == ((), "updates", "string_data")


# ---------------------------------------------------------------------------
# RemoteAgent — aget_state
# ---------------------------------------------------------------------------


class TestRemoteAgentGetState:
    async def test_returns_state_on_success(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        state = MagicMock(values={"messages": []}, next=())
        mock_graph.aget_state = AsyncMock(return_value=state)
        agent._graph = mock_graph

        result = await agent.aget_state(_config())
        assert result is state

    async def test_raises_when_thread_id_missing(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        with pytest.raises(ValueError, match="thread_id"):
            await agent.aget_state({"configurable": {}})

    async def test_returns_none_on_not_found(self) -> None:
        from langgraph_sdk.errors import NotFoundError

        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        request = MagicMock()
        response = MagicMock(status_code=404, headers={})
        exc = NotFoundError("not found", response=response, body=None)
        exc.request = request
        mock_graph.aget_state = AsyncMock(side_effect=exc)
        agent._graph = mock_graph

        result = await agent.aget_state(_config())
        assert result is None

    async def test_propagates_non_404_exception(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.aget_state = AsyncMock(side_effect=ConnectionError("down"))
        agent._graph = mock_graph

        with pytest.raises(ConnectionError, match="down"):
            await agent.aget_state(_config())

    async def test_normalizes_config(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.aget_state = AsyncMock(return_value=None)
        agent._graph = mock_graph

        await agent.aget_state({"configurable": {"thread_id": _TEST_THREAD_ID}})
        call_config = mock_graph.aget_state.call_args[0][0]
        uuid.UUID(call_config["configurable"]["thread_id"])


# ---------------------------------------------------------------------------
# RemoteAgent — aupdate_state
# ---------------------------------------------------------------------------


class TestRemoteAgentUpdateState:
    async def test_delegates_to_graph(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.aupdate_state = AsyncMock()
        agent._graph = mock_graph

        await agent.aupdate_state(_config(), {"key": "val"})
        mock_graph.aupdate_state.assert_called_once()

    async def test_raises_when_thread_id_missing(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        with pytest.raises(ValueError, match="thread_id"):
            await agent.aupdate_state({"configurable": {}}, {"key": "val"})

    async def test_propagates_exception(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.aupdate_state = AsyncMock(side_effect=ConnectionError("down"))
        agent._graph = mock_graph

        with pytest.raises(ConnectionError, match="down"):
            await agent.aupdate_state(_config(), {"key": "val"})

    async def test_normalizes_config(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.aupdate_state = AsyncMock()
        agent._graph = mock_graph

        await agent.aupdate_state(
            {"configurable": {"thread_id": _TEST_THREAD_ID}}, {"key": "val"}
        )
        call_config = mock_graph.aupdate_state.call_args[0][0]
        uuid.UUID(call_config["configurable"]["thread_id"])


class TestRemoteAgentEnsureThread:
    """Verify remote thread registration before state writes."""

    async def test_creates_thread_with_do_nothing(self) -> None:
        """Creates the remote thread idempotently before cold-resume updates."""
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_threads = MagicMock()
        mock_threads.create = AsyncMock()
        mock_client = MagicMock()
        mock_client.threads = mock_threads
        mock_graph = MagicMock()
        mock_graph._validate_client.return_value = mock_client
        agent._graph = mock_graph

        await agent.aensure_thread(
            {
                "configurable": {"thread_id": _TEST_THREAD_ID},
                "metadata": {"assistant_id": "agent"},
            }
        )

        kwargs = mock_threads.create.call_args.kwargs
        uuid.UUID(kwargs["thread_id"])
        assert kwargs["if_exists"] == "do_nothing"
        assert kwargs["metadata"] == {"assistant_id": "agent"}
        assert kwargs["graph_id"] == "agent"

    async def test_raises_when_thread_id_missing(self) -> None:
        """Rejects ensure-thread calls that omit `configurable.thread_id`."""
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")

        with pytest.raises(ValueError, match="thread_id"):
            await agent.aensure_thread({"configurable": {}})


# ---------------------------------------------------------------------------
# RemoteAgent — with_config
# ---------------------------------------------------------------------------


class TestRemoteAgentInit:
    def test_api_key_passed_to_remote_graph(self) -> None:
        """api_key kwarg is forwarded to RemoteGraph."""
        agent = RemoteAgent(
            url="http://localhost:8123",
            graph_name="agent",
            api_key="sk-test-123",
        )
        with patch("langgraph.pregel.remote.RemoteGraph") as mock_cls:
            agent._get_graph()
            args, kwargs = mock_cls.call_args
            assert args == ("agent",)
            assert kwargs["url"] == "http://localhost:8123"
            assert kwargs["api_key"] == "sk-test-123"
            assert kwargs["headers"] is None
            assert kwargs["client"] is not None
            assert kwargs["sync_client"] is not None

    def test_headers_passed_to_remote_graph(self) -> None:
        """Headers kwarg is forwarded to RemoteGraph."""
        hdrs = {"Authorization": "Bearer tok", "X-Custom": "val"}
        agent = RemoteAgent(
            url="http://localhost:8123",
            graph_name="agent",
            headers=hdrs,
        )
        with patch("langgraph.pregel.remote.RemoteGraph") as mock_cls:
            agent._get_graph()
            _, kwargs = mock_cls.call_args
            assert kwargs["headers"] == hdrs

    def test_defaults_no_auth(self) -> None:
        """Default construction passes None for api_key and headers."""
        agent = RemoteAgent(url="http://localhost:8123")
        with patch("langgraph.pregel.remote.RemoteGraph") as mock_cls:
            agent._get_graph()
            _, kwargs = mock_cls.call_args
            assert kwargs["api_key"] is None
            assert kwargs["headers"] is None

    def test_graph_lazy_singleton(self) -> None:
        """_get_graph creates RemoteGraph once and caches it."""
        agent = RemoteAgent(url="http://localhost:8123")
        with patch("langgraph.pregel.remote.RemoteGraph") as mock_cls:
            g1 = agent._get_graph()
            g2 = agent._get_graph()
            assert g1 is g2
            mock_cls.assert_called_once()

    def test_extended_read_timeout_applied_to_clients(self) -> None:
        """Default 1800s read timeout is configured on the underlying httpx clients.

        The langgraph_sdk default is 300s; we extend it so /review-style turns
        don't get killed mid-stream by the default deadline.
        """
        agent = RemoteAgent(url="http://localhost:8123")
        with (
            patch("langgraph.pregel.remote.RemoteGraph"),
            patch("langgraph_sdk.client.get_client") as mock_async,
            patch("langgraph_sdk.client.get_sync_client") as mock_sync,
        ):
            agent._get_graph()
            for mock in (mock_async, mock_sync):
                _, kwargs = mock.call_args
                timeout = kwargs.get("timeout")
                assert timeout is not None
                assert timeout.read == 1800.0

    def test_read_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BOG_AGENTS_REMOTE_READ_TIMEOUT overrides the default."""
        monkeypatch.setenv("BOG_AGENTS_REMOTE_READ_TIMEOUT", "60")
        agent = RemoteAgent(url="http://localhost:8123")
        with (
            patch("langgraph.pregel.remote.RemoteGraph"),
            patch("langgraph_sdk.client.get_client") as mock_async,
            patch("langgraph_sdk.client.get_sync_client"),
        ):
            agent._get_graph()
            _, kwargs = mock_async.call_args
            assert kwargs["timeout"].read == 60.0

    def test_read_timeout_env_disable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`none` in BOG_AGENTS_REMOTE_READ_TIMEOUT disables the deadline."""
        monkeypatch.setenv("BOG_AGENTS_REMOTE_READ_TIMEOUT", "none")
        agent = RemoteAgent(url="http://localhost:8123")
        with (
            patch("langgraph.pregel.remote.RemoteGraph"),
            patch("langgraph_sdk.client.get_client") as mock_async,
            patch("langgraph_sdk.client.get_sync_client"),
        ):
            agent._get_graph()
            _, kwargs = mock_async.call_args
            assert kwargs["timeout"].read is None


class TestRemoteAgentWithConfig:
    def test_returns_self(self) -> None:
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        assert agent.with_config({"configurable": {}}) is agent


class TestRemoteAgentTransientRetry:
    """Pre-first-event retry on ReadTimeoutError / transient SSE errors.

    Once events have flowed, retrying is unsafe; the stream must propagate.
    Before first event, we re-issue the call up to `_PRE_FIRST_EVENT_RETRIES`
    times since the server hasn't begun mutating thread state.
    """

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_when_first_attempt_times_out(self) -> None:
        from bog_agents_cli import remote_client as rc

        calls: list[int] = []

        async def _gen(*_a: Any, **_kw: Any):  # async generator
            calls.append(1)
            if len(calls) == 1:
                msg = "ReadTimeoutError mid-handshake"
                raise TimeoutError(msg)
            yield ((), "updates", {"ok": True})

        config = {"configurable": {"thread_id": _TEST_THREAD_ID}}
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.astream = _gen
        agent._graph = mock_graph

        with patch.object(rc.asyncio, "sleep", AsyncMock()):
            events = [chunk async for chunk in agent.astream({}, config=config)]

        assert len(calls) == 2
        assert events == [((), "updates", {"ok": True})]

    @pytest.mark.asyncio
    async def test_does_not_retry_after_first_event_emitted(self) -> None:
        from bog_agents_cli import remote_client as rc

        async def _gen(*_a: Any, **_kw: Any):  # async generator
            yield ((), "updates", {"started": True})
            msg = "ReadTimeoutError partway through"
            raise TimeoutError(msg)

        config = {"configurable": {"thread_id": _TEST_THREAD_ID}}
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.astream = _gen
        agent._graph = mock_graph

        with (
            patch.object(rc.asyncio, "sleep", AsyncMock()),
            pytest.raises(TimeoutError),
        ):
            async for _ in agent.astream({}, config=config):
                pass

    @pytest.mark.asyncio
    async def test_non_transient_error_propagates_immediately(self) -> None:
        from bog_agents_cli import remote_client as rc

        async def _gen(*_a: Any, **_kw: Any):  # async generator
            msg = "bad input"
            raise ValueError(msg)
            yield  # pragma: no cover  # makes this a generator

        config = {"configurable": {"thread_id": _TEST_THREAD_ID}}
        agent = RemoteAgent(url="http://localhost:8123", graph_name="agent")
        mock_graph = MagicMock()
        mock_graph.astream = _gen
        agent._graph = mock_graph

        with (
            patch.object(rc.asyncio, "sleep", AsyncMock()),
            pytest.raises(ValueError, match="bad input"),
        ):
            async for _ in agent.astream({}, config=config):
                pass


class TestTransientErrorClassifier:
    def test_readtimeout_is_transient(self) -> None:
        from bog_agents_cli.remote_client import _is_transient_stream_error

        assert _is_transient_stream_error(TimeoutError("ReadTimeoutError sock"))
        assert _is_transient_stream_error(
            RuntimeError("RemoteException: ReadTimeoutError")
        )

    def test_connecterror_is_transient(self) -> None:
        from bog_agents_cli.remote_client import _is_transient_stream_error

        assert _is_transient_stream_error(ConnectionError("network blip"))

    def test_value_error_is_not_transient(self) -> None:
        from bog_agents_cli.remote_client import _is_transient_stream_error

        assert not _is_transient_stream_error(ValueError("schema mismatch"))
