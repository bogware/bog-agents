"""Remote agent client — thin wrapper around LangGraph's `RemoteGraph`.

Delegates streaming, state management, and SSE handling to
`langgraph.pregel.remote.RemoteGraph`. The only added logic is converting raw
message dicts from the server into LangChain message objects that the CLI's
Textual adapter expects.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

from bog_agents_cli._debug import configure_debug_logging

logger = logging.getLogger(__name__)
configure_debug_logging(logger)

# Default read timeout for the langgraph SDK's underlying httpx client.
# The SDK default is 300s, which is too tight for /review-style turns that
# can fan out to many tool calls and span 5-20 minutes of model work.
# Aligned with ``BOG_AGENTS_MODEL_READ_TIMEOUT`` default (3600s) so the SSE
# deadline never fires before the underlying model call has had its full
# budget. Override with ``BOG_AGENTS_REMOTE_READ_TIMEOUT`` (seconds, or
# ``none``/``0`` to disable).
_DEFAULT_READ_TIMEOUT_SECS: float = 3600.0

# Number of times to re-issue an astream() call when the SSE stream raises a
# transient error *before any events have flowed*. Once events have been
# emitted, retrying is unsafe (the server has already mutated state).
_PRE_FIRST_EVENT_RETRIES: int = 2

# Substrings that indicate a transient (network/provider) failure worth
# retrying when raised before the first event. Matched case-insensitively
# against the exception's class name and string form.
_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "readtimeout",
    "connecttimeout",
    "connectionerror",
    "remoteprotocolerror",
    "incompletewrite",
)


def _resolve_read_timeout() -> float | None:
    """Resolve the SSE read timeout from env var or default.

    Returns:
        Read timeout in seconds, or `None` to disable the read deadline.
    """
    raw = os.environ.get("BOG_AGENTS_REMOTE_READ_TIMEOUT")
    if raw is None:
        return _DEFAULT_READ_TIMEOUT_SECS
    if raw.strip().lower() in {"none", "off", "0", ""}:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid BOG_AGENTS_REMOTE_READ_TIMEOUT=%r, using default %ss",
            raw,
            _DEFAULT_READ_TIMEOUT_SECS,
        )
        return _DEFAULT_READ_TIMEOUT_SECS
    if value <= 0:
        return None
    return value


_DROPPED = object()
"""Sentinel yielded by `_process_chunk` when a message failed conversion."""


def _is_transient_stream_error(exc: BaseException) -> bool:
    """Return True if `exc` looks like a network/provider blip worth retrying.

    Args:
        exc: Exception raised from the SSE stream.

    Returns:
        True if the exception name or message contains a transient marker.
    """
    haystack = f"{type(exc).__name__} {exc!s}".lower()
    return any(marker in haystack for marker in _TRANSIENT_ERROR_MARKERS)


def _require_thread_id(config: dict[str, Any] | None) -> str:
    """Extract and validate that `thread_id` is present in config.

    Args:
        config: Config dict with `configurable.thread_id`.

    Returns:
        The thread ID string.

    Raises:
        ValueError: If `thread_id` is missing.
    """
    thread_id = (config or {}).get("configurable", {}).get("thread_id")
    if not thread_id:
        msg = "thread_id is required in config.configurable"
        raise ValueError(msg)
    return thread_id


class RemoteAgent:
    """Client that talks to a LangGraph server over HTTP+SSE.

    Wraps `langgraph.pregel.remote.RemoteGraph` which handles SSE parsing,
    stream-mode negotiation (`messages-tuple`), namespace extraction, and
    interrupt detection. This class adds only message-object conversion for the
    Textual adapter and thread-ID normalization.
    """

    def __init__(
        self,
        url: str,
        *,
        graph_name: str = "agent",
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        read_timeout: float | None = None,
    ) -> None:
        """Initialize the remote agent client.

        Args:
            url: Base URL of the LangGraph server.
            graph_name: Name of the graph on the server.
            api_key: API key for authenticated deployments.

                When `None`, `RemoteGraph` auto-reads `LANGGRAPH_API_KEY`,
                `LANGSMITH_API_KEY`, or `LANGCHAIN_API_KEY` from
                the environment.
            headers: Extra HTTP headers to include in every request
                (e.g. bearer tokens, proxy headers).
            read_timeout: SSE read timeout in seconds. When `None`, the
                value from `BOG_AGENTS_REMOTE_READ_TIMEOUT` is used,
                falling back to `_DEFAULT_READ_TIMEOUT_SECS` (1800s) when
                that env var is unset. Set the env var to `none`/`0` to
                disable the read deadline entirely.
        """
        self._url = url
        self._graph_name = graph_name
        self._api_key = api_key
        self._headers = headers
        self._read_timeout = (
            read_timeout if read_timeout is not None else _resolve_read_timeout()
        )
        if self._read_timeout is not None and self._read_timeout <= 0:
            self._read_timeout = None
        self._graph: Any = None

    def _get_graph(self) -> Any:  # noqa: ANN401
        """Lazily create the `RemoteGraph` instance.

        The underlying httpx clients are constructed with an extended read
        timeout so long-running review/refactor turns don't get killed by
        the langgraph_sdk's 300s default mid-stream.

        Returns:
            A `RemoteGraph` connected to the server.
        """
        if self._graph is None:
            import httpx
            from langgraph.pregel.remote import RemoteGraph
            from langgraph_sdk.client import get_client, get_sync_client

            # langgraph_sdk default is httpx.Timeout(connect=5, read=300,
            # write=300, pool=5). Bump read+write so SSE long-polls and
            # uploads can survive multi-minute provider stalls.
            read = self._read_timeout
            write = read
            timeout = httpx.Timeout(connect=5.0, read=read, write=write, pool=5.0)

            client = get_client(
                url=self._url,
                api_key=self._api_key,
                headers=self._headers,
                timeout=timeout,
            )
            sync_client = get_sync_client(
                url=self._url,
                api_key=self._api_key,
                headers=self._headers,
                timeout=timeout,
            )
            self._graph = RemoteGraph(
                self._graph_name,
                url=self._url,
                api_key=self._api_key,
                headers=self._headers,
                client=client,
                sync_client=sync_client,
            )
        return self._graph

    async def astream(
        self,
        input: dict | Any,  # noqa: A002, ANN401
        *,
        stream_mode: list[str] | None = None,
        subgraphs: bool = False,
        config: dict[str, Any] | None = None,
        context: Any | None = None,  # noqa: ANN401
        durability: str | None = None,  # noqa: ARG002
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        """Stream agent execution, yielding tuples matching Pregel's format.

        Delegates to `RemoteGraph.astream` (which handles `messages-tuple`
        negotiation, SSE routing, and namespace parsing) and converts the raw
        message dicts into LangChain message objects for the adapter.

        Args:
            input: The input to send (messages dict or Command).
            stream_mode: Stream modes to request.
            subgraphs: Whether to stream subgraph events.
            config: LangGraph config with `configurable.thread_id`, etc.
            context: Runtime context (e.g. `CLIContext`) forwarded to the
                server via the SDK's `context=` parameter.
            durability: Ignored (server manages durability).

        Yields:
            3-tuples of `(namespace, stream_mode, data)`.

        Raises:
            ValueError: If `thread_id` is not present in `config`.
        """  # noqa: DOC502 — raised by _require_thread_id
        from langchain_core.messages import BaseMessage

        _require_thread_id(config)

        graph = self._get_graph()
        config = _prepare_config(config)
        dropped_count = 0

        modes = stream_mode or ["messages", "updates"]
        first_event_seen = False
        attempt = 0
        max_attempts = 1 + _PRE_FIRST_EVENT_RETRIES

        while True:
            attempt += 1
            try:
                async for ns, mode, data in graph.astream(
                    input,
                    stream_mode=modes,
                    subgraphs=subgraphs,
                    config=config,
                    context=context,
                ):
                    first_event_seen = True
                    async for converted in RemoteAgent._process_chunk(
                        ns, mode, data, BaseMessage
                    ):
                        if converted is _DROPPED:
                            dropped_count += 1
                        else:
                            yield converted
                break
            except Exception as exc:
                if (
                    not first_event_seen
                    and attempt < max_attempts
                    and _is_transient_stream_error(exc)
                ):
                    backoff = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        "Transient %s before first stream event "
                        "(attempt %d/%d); retrying in %.1fs",
                        type(exc).__name__,
                        attempt,
                        max_attempts,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise

        if dropped_count:
            logger.warning(
                "Dropped %d message(s) during stream due to conversion failures",
                dropped_count,
            )

    @staticmethod
    async def _process_chunk(
        ns: tuple[str, ...],
        mode: str,
        data: Any,  # noqa: ANN401
        base_message_cls: type,
    ) -> AsyncIterator[Any]:
        """Convert a single RemoteGraph chunk into adapter-ready events.

        Yields adapter-format 3-tuples, or the `_DROPPED` sentinel when a
        message could not be deserialized.

        Args:
            ns: Namespace tuple from RemoteGraph.
            mode: Stream mode (`messages`, `updates`, etc.).
            data: Raw event payload.
            base_message_cls: `langchain_core.messages.BaseMessage` (passed
                in to avoid re-importing on every chunk).

        Yields:
            Either a fully-converted 3-tuple ready for the adapter, or the
            `_DROPPED` sentinel for messages that failed conversion.
        """
        logger.debug("RemoteGraph event mode=%s ns=%s", mode, ns)

        if mode == "messages":
            msg_dict, meta = data
            if isinstance(msg_dict, dict):
                msg_obj = _convert_message_data(msg_dict)
                if msg_obj is not None:
                    yield (ns, "messages", (msg_obj, meta or {}))
                else:
                    yield _DROPPED
            elif isinstance(msg_dict, base_message_cls):
                yield (ns, "messages", (msg_dict, meta or {}))
            else:
                logger.warning(
                    "Unexpected message data type in stream: %s",
                    type(msg_dict).__name__,
                )
            return

        if mode == "updates" and isinstance(data, dict):
            update_data = data
            if "__interrupt__" in data:
                update_data = {
                    **data,
                    "__interrupt__": _convert_interrupts(data["__interrupt__"]),
                }
            yield (ns, "updates", update_data)
            return

        yield (ns, mode, data)

    async def aget_state(
        self,
        config: dict[str, Any],
    ) -> Any:  # noqa: ANN401
        """Get the current state of a thread.

        Returns `None` when the thread does not exist on the server (404).
        All other errors (network, auth, 500) are logged at WARNING and
        re-raised so callers can handle them.

        Args:
            config: Config with `configurable.thread_id`.

        Returns:
            Thread state object with `values` and `next` attributes, or `None`
                if the thread is not found.

        Raises:
            ValueError: If `thread_id` is not present in `config`.
        """  # noqa: DOC502 — raised by _require_thread_id
        from langgraph_sdk.errors import NotFoundError

        thread_id = _require_thread_id(config)

        graph = self._get_graph()
        try:
            return await graph.aget_state(_prepare_config(config))
        except NotFoundError:
            logger.debug("Thread %s not found on server", thread_id)
            return None
        except Exception:
            logger.warning(
                "Failed to get state for thread %s", thread_id, exc_info=True
            )
            raise

    async def aupdate_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        """Update the state of a thread.

        Exceptions from the underlying graph (server/network errors) are logged
        at WARNING level and then re-raised so callers can handle them.

        Args:
            config: Config with `configurable.thread_id`.
            values: State values to update.

        Raises:
            ValueError: If `thread_id` is not present in `config`.
        """  # noqa: DOC502 — raised by _require_thread_id
        thread_id = _require_thread_id(config)

        graph = self._get_graph()
        try:
            await graph.aupdate_state(_prepare_config(config), values)
        except Exception:
            logger.warning(
                "Failed to update state for thread %s", thread_id, exc_info=True
            )
            raise

    async def aensure_thread(self, config: dict[str, Any]) -> None:
        """Ensure the remote thread record exists before mutating state.

        In the LangGraph dev server, checkpoint persistence and HTTP thread
        registration are separate. After a server restart, a thread may still
        have checkpointed state on disk while `POST /threads/{id}/state`
        returns 404 because the server has not yet materialized that thread in
        its live store.

        This method performs the idempotent HTTP-side registration with
        `if_exists='do_nothing'` so callers that recovered state from
        persistence can safely follow up with `aupdate_state`.

        Args:
            config: Config with `configurable.thread_id` and optional metadata.

        Raises:
            ValueError: If `thread_id` is not present in `config`.
        """  # noqa: DOC502 — raised by _require_thread_id
        _require_thread_id(config)

        graph = self._get_graph()
        prepared = _prepare_config(config)
        thread_id = prepared["configurable"]["thread_id"]
        metadata = prepared.get("metadata")
        thread_metadata = metadata if isinstance(metadata, dict) else None

        try:
            client = graph._validate_client()
            await client.threads.create(
                thread_id=thread_id,
                if_exists="do_nothing",
                metadata=thread_metadata,
                graph_id=self._graph_name,
            )
        except Exception:
            logger.warning(
                "Failed to ensure thread %s exists on remote server",
                thread_id,
                exc_info=True,
            )
            raise

    def with_config(self, config: dict[str, Any]) -> RemoteAgent:  # noqa: ARG002
        """Return self (config is passed per-call, not stored).

        Args:
            config: Ignored.

        Returns:
            Self.
        """
        return self


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _prepare_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow-copy config so callers' dicts are not mutated.

    Args:
        config: Raw config dict.

    Returns:
        A shallow copy of the config.
    """
    config = dict(config or {})
    configurable = dict(config.get("configurable", {}))
    config["configurable"] = configurable
    return config


def _convert_interrupts(raw: Any) -> list[Any]:  # noqa: ANN401
    """Convert interrupt dicts from the server into Interrupt objects.

    Args:
        raw: List of interrupt dicts or Interrupt objects from the server.

    Returns:
        List of Interrupt objects.
    """
    from langgraph.types import Interrupt

    if not isinstance(raw, list):
        logger.warning(
            "Expected list for __interrupt__ data, got %s",
            type(raw).__name__,
        )
        return [raw] if raw is not None else []
    results = []
    for item in raw:
        if isinstance(item, Interrupt):
            results.append(item)
        elif isinstance(item, dict) and "value" in item:
            results.append(Interrupt(value=item["value"], id=item.get("id", "")))
        else:
            results.append(item)
    return results


# ---------------------------------------------------------------------------
# Message conversion — per-type converters with a dispatch table
# ---------------------------------------------------------------------------
#
# Each converter handles one LangChain message type.  The dispatch table
# maps type strings (both short and class-name forms) to the appropriate
# converter.  This keeps each converter focused and makes adding new
# message types a one-line addition to the table.
# ---------------------------------------------------------------------------


def _convert_ai_message(data: dict[str, Any]) -> Any:  # noqa: ANN401
    """Convert a server AI message dict to an `AIMessageChunk`.

    Handles the three tool-call representations the server may emit:

    - `tool_call_chunks`: streaming partial args (string `args`).
    - `tool_calls` with string `args`: legacy streaming format,
        normalized to `tool_call_chunks`.
    - `tool_calls` with dict `args`: fully parsed calls.

    Args:
        data: Raw message dict from the server.

    Returns:
        An `AIMessageChunk`, or `None` on construction failure.
    """
    from langchain_core.messages import AIMessageChunk

    content = data.get("content", "")
    tool_call_chunks = data.get("tool_call_chunks", [])
    tool_calls = data.get("tool_calls", [])
    usage_metadata = data.get("usage_metadata")
    response_metadata = data.get("response_metadata", {})

    kwargs: dict[str, Any] = {
        "content": content,
        "id": data.get("id"),
        "response_metadata": response_metadata,
    }

    if tool_call_chunks:
        kwargs["tool_call_chunks"] = [
            {
                "name": tc.get("name"),
                "args": tc.get("args", ""),
                "id": tc.get("id"),
                "index": tc.get("index", i),
            }
            for i, tc in enumerate(tool_call_chunks)
        ]
    elif tool_calls:
        has_str_args = any(isinstance(tc.get("args"), str) for tc in tool_calls)
        if has_str_args:
            kwargs["tool_call_chunks"] = [
                {
                    "name": tc.get("name"),
                    "args": tc.get("args", ""),
                    "id": tc.get("id"),
                    "index": i,
                }
                for i, tc in enumerate(tool_calls)
            ]
        else:
            kwargs["tool_calls"] = tool_calls

    try:
        chunk = AIMessageChunk(**kwargs)
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct AIMessageChunk from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None

    if usage_metadata:
        chunk.usage_metadata = usage_metadata
    return chunk


def _convert_human_message(data: dict[str, Any]) -> Any:  # noqa: ANN401
    """Convert a server human message dict to a `HumanMessage`.

    Args:
        data: Raw message dict from the server.

    Returns:
        A `HumanMessage`, or `None` on construction failure.
    """
    from langchain_core.messages import HumanMessage

    try:
        return HumanMessage(
            content=data.get("content", ""),
            id=data.get("id"),
        )
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct HumanMessage from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None


def _convert_tool_message(data: dict[str, Any]) -> Any:  # noqa: ANN401
    """Convert a server tool message dict to a `ToolMessage`.

    Args:
        data: Raw message dict from the server.

    Returns:
        A `ToolMessage`, or `None` on construction failure.
    """
    from langchain_core.messages import ToolMessage

    try:
        return ToolMessage(
            content=data.get("content", ""),
            tool_call_id=data.get("tool_call_id", ""),
            name=data.get("name", ""),
            id=data.get("id"),
            status=data.get("status", "success"),
        )
    except (TypeError, ValueError, KeyError):
        logger.warning(
            "Failed to construct ToolMessage from server data (id=%s)",
            data.get("id"),
            exc_info=True,
        )
        return None


_MESSAGE_CONVERTERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "ai": _convert_ai_message,
    "AIMessage": _convert_ai_message,
    "AIMessageChunk": _convert_ai_message,
    "human": _convert_human_message,
    "HumanMessage": _convert_human_message,
    "tool": _convert_tool_message,
    "ToolMessage": _convert_tool_message,
}
"""Maps server message `type` strings to their converter functions.

Both short forms (`'ai'`, `'human'`, `'tool'`) and class-name forms
(`'AIMessage'`, `'HumanMessage'`, `'ToolMessage'`) are supported so
the converter works regardless of how the server serializes the type field.
"""


def _convert_message_data(data: dict[str, Any]) -> Any:  # noqa: ANN401
    """Convert a server message dict into a LangChain message object.

    Dispatches to a per-type converter via `_MESSAGE_CONVERTERS`. New message
    types can be supported by adding a converter function and a table entry —
    no changes to this dispatcher are needed.

    Args:
        data: Message dict from the server.

    Returns:
        A LangChain message object, or `None` if conversion fails.
    """
    msg_type = data.get("type", "")
    converter = _MESSAGE_CONVERTERS.get(msg_type)
    if converter is not None:
        return converter(data)
    logger.warning("Unknown message type in stream: %s", msg_type)
    return None
