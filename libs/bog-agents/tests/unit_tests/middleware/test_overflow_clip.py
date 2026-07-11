"""Tests for the `ContextOverflowError` tail-clip fallback in `SummarizationMiddleware`.

The headline regression these guard: before `_overflow_clip` existed, a
`ContextOverflowError` sent the agent straight back to the model with the *same*
oversized tail, so the retry overflowed again and the agent wedged permanently.
`test_overflow_retry_sends_smaller_payload` asserts the second attempt carries a
strictly smaller payload than the first.
"""

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from bog_agents.backends.protocol import BackendProtocol, EditResult, FileDownloadResponse, FileUploadResponse, WriteResult
from bog_agents.middleware._message_eviction import (
    TOO_LARGE_TOOL_MSG,
    _build_evicted_tool_message,
    _extract_text_from_message,
)
from bog_agents.middleware._overflow_clip import (
    DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS,
    _build_tool_call_index,
    _clip_overflow_tail,
    _derive_overflow_clip_threshold_tokens,
    _find_tail_tool_message_batch,
    _read_file_original_path,
)
from bog_agents.middleware.summarization import SummarizationMiddleware

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentState

# A tail large enough to clear the 5k-token default clip threshold
# (`chars / 4` approximation => 40_000 chars is ~10_000 tokens).
HUGE_TOOL_RESULT = "\n".join(f"line {i}: " + ("x" * 60) for i in range(600))


class RecordingBackend(BackendProtocol):
    """Backend that records writes and can fail them on demand."""

    def __init__(self, *, write_fails: bool = False, upload_not_implemented: bool = False) -> None:
        """Initialize the recording backend.

        Args:
            write_fails: If `True`, every `write` returns an error result.
            upload_not_implemented: If `True`, `upload_files` raises
                `NotImplementedError` (as `StateBackend` historically did).
        """
        self.write_calls: list[tuple[str, str]] = []
        self.upload_calls: list[str] = []
        self._write_fails = write_fails
        self._upload_not_implemented = upload_not_implemented

    def write(self, path: str, content: str) -> WriteResult:
        self.write_calls.append((path, content))
        if self._write_fails:
            return WriteResult(error="disk full")
        return WriteResult(path=path)

    async def awrite(self, path: str, content: str) -> WriteResult:
        return self.write(path, content)

    def edit(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(path=path, occurrences=1)

    async def aedit(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self.edit(path, old_string, new_string, replace_all)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=p, content=None, error="file_not_found") for p in paths]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self.download_files(paths)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        if self._upload_not_implemented:
            raise NotImplementedError
        self.upload_calls.extend(path for path, _ in files)
        return [FileUploadResponse(path=path, error=None) for path, _ in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)

    @property
    def large_tool_result_writes(self) -> list[tuple[str, str]]:
        """Writes that landed under the offloaded-tool-results prefix."""
        return [call for call in self.write_calls if call[0].startswith("/large_tool_results/")]


def make_mock_model() -> MagicMock:
    """Create a mock chat model whose `invoke` returns a summary."""
    model = MagicMock()
    model.invoke.return_value = MagicMock(text="A summary.")
    model.ainvoke = MagicMock()
    model._llm_type = "test-model"
    model.profile = {"max_input_tokens": 100_000}
    model._get_ls_params.return_value = {"ls_provider": "test"}
    return model


def make_overflow_conversation(*, tool_name: str = "search", tool_args: dict[str, Any] | None = None) -> list[BaseMessage]:
    """Build a conversation whose trailing `ToolMessage` is oversized.

    Args:
        tool_name: Name of the tool that produced the trailing result.
        tool_args: Args recorded on the originating tool call.

    Returns:
        A five-message conversation ending in an `AIMessage`/`ToolMessage` pair.
    """
    return [
        HumanMessage(content="hello", id="h0"),
        AIMessage(content="hi there", id="a0"),
        HumanMessage(content="search the repo", id="h1"),
        AIMessage(
            content="on it",
            id="a1",
            tool_calls=[{"id": "tc-1", "name": tool_name, "args": tool_args or {"query": "x"}}],
        ),
        ToolMessage(content=HUGE_TOOL_RESULT, tool_call_id="tc-1", id="tm-1"),
    ]


def make_middleware(backend: BackendProtocol) -> SummarizationMiddleware:
    """Build a middleware that never auto-summarizes, so only overflow triggers it."""
    return SummarizationMiddleware(
        model=make_mock_model(),
        backend=backend,
        # Absurdly high so `_should_summarize` is always False: the ONLY way into
        # the summarization path is the ContextOverflowError fallback.
        trigger=("tokens", 10_000_000),
        keep=("messages", 2),
    )


def make_request(messages: list[BaseMessage]) -> ModelRequest:
    """Build a `ModelRequest` around `messages`."""
    runtime = MagicMock()
    runtime.context = {}
    runtime.stream_writer = MagicMock()
    runtime.store = None
    del runtime.config
    state = cast("AgentState[Any]", {"messages": messages})
    return ModelRequest(
        model=make_mock_model(),
        messages=messages,
        system_message=None,
        tools=[],
        runtime=runtime,
        state=state,
    )


class OverflowThenSucceed:
    """Handler that raises `ContextOverflowError` on the first call only."""

    def __init__(self) -> None:
        """Initialize with an empty payload log."""
        self.payloads: list[list[AnyMessage]] = []

    def __call__(self, request: ModelRequest) -> ModelResponse:
        self.payloads.append(list(request.messages))
        if len(self.payloads) == 1:
            msg = "context window exceeded"
            raise ContextOverflowError(msg)
        return cast("ModelResponse", AIMessage(content="ok"))

    async def acall(self, request: ModelRequest) -> ModelResponse:
        return self(request)


class TestOverflowRetryShrinksPayload:
    """The core regression: a retry after overflow must send less, not the same."""

    def test_overflow_retry_sends_smaller_payload(self) -> None:
        """Second attempt after `ContextOverflowError` must carry a smaller payload."""
        backend = RecordingBackend()
        middleware = make_middleware(backend)
        handler = OverflowThenSucceed()

        result = middleware.wrap_model_call(make_request(make_overflow_conversation()), handler)

        assert len(handler.payloads) == 2, "handler must be retried exactly once after overflow"
        first_tokens = count_tokens_approximately(handler.payloads[0])
        second_tokens = count_tokens_approximately(handler.payloads[1])
        assert second_tokens < first_tokens, f"retry payload ({second_tokens} tokens) must shrink below the overflowing one ({first_tokens})"

        # The oversized tail specifically is what shrank -- it was offloaded.
        assert backend.large_tool_result_writes == [("/large_tool_results/tc-1", HUGE_TOOL_RESULT)]
        retried_tail = handler.payloads[1][-1]
        assert isinstance(retried_tail, ToolMessage)
        assert "/large_tool_results/tc-1" in _extract_text_from_message(retried_tail)
        assert HUGE_TOOL_RESULT not in _extract_text_from_message(retried_tail)

        assert isinstance(result, ExtendedModelResponse)

    async def test_async_overflow_retry_sends_smaller_payload(self) -> None:
        """Async twin of the sync smaller-payload assertion."""
        backend = RecordingBackend()
        middleware = make_middleware(backend)
        handler = OverflowThenSucceed()

        result = await middleware.awrap_model_call(make_request(make_overflow_conversation()), handler.acall)

        assert len(handler.payloads) == 2
        assert count_tokens_approximately(handler.payloads[1]) < count_tokens_approximately(handler.payloads[0])
        assert backend.large_tool_result_writes == [("/large_tool_results/tc-1", HUGE_TOOL_RESULT)]
        assert isinstance(result, ExtendedModelResponse)

    def test_clipped_tail_written_to_state_with_original_ids(self) -> None:
        """The state update reuses original message ids so `add_messages` overwrites in place."""
        backend = RecordingBackend()
        middleware = make_middleware(backend)

        result = middleware.wrap_model_call(make_request(make_overflow_conversation()), OverflowThenSucceed())

        assert isinstance(result, ExtendedModelResponse)
        assert result.command is not None
        assert result.command.update is not None
        state_messages = result.command.update["messages"]
        # Exactly one replacement, carrying the ORIGINAL id -- so the reducer
        # overwrites rather than appending. Message count and order are unchanged.
        assert [m.id for m in state_messages] == ["tm-1"]
        assert [m.tool_call_id for m in state_messages] == ["tc-1"]

    def test_no_state_write_when_nothing_clipped(self) -> None:
        """A small tail is left alone and produces no `messages` state update."""
        backend = RecordingBackend()
        middleware = make_middleware(backend)
        messages = make_overflow_conversation()
        messages[-1] = ToolMessage(content="tiny result", tool_call_id="tc-1", id="tm-1")
        handler = OverflowThenSucceed()

        result = middleware.wrap_model_call(make_request(messages), handler)

        assert isinstance(result, ExtendedModelResponse)
        assert result.command is not None
        assert result.command.update is not None
        assert "messages" not in result.command.update
        assert backend.large_tool_result_writes == []
        # The retried tail is byte-identical to the original.
        assert handler.payloads[1][-1].content == "tiny result"

    def test_read_file_tail_is_sliced_not_offloaded(self) -> None:
        """A `read_file` result is head-sliced and points back at its original path."""
        backend = RecordingBackend()
        middleware = make_middleware(backend)
        messages = make_overflow_conversation(tool_name="read_file", tool_args={"file_path": "/repo/big.txt"})
        handler = OverflowThenSucceed()

        middleware.wrap_model_call(make_request(messages), handler)

        # No new offload write: the full content already lives at /repo/big.txt.
        assert backend.large_tool_result_writes == []
        retried_tail = handler.payloads[1][-1]
        text = _extract_text_from_message(cast("ToolMessage", retried_tail))
        assert "/repo/big.txt" in text
        assert len(text) < len(HUGE_TOOL_RESULT)
        assert count_tokens_approximately(handler.payloads[1]) < count_tokens_approximately(handler.payloads[0])

    def test_failed_backend_write_keeps_original_message(self) -> None:
        """When the offload write fails, the original tail survives untouched."""
        backend = RecordingBackend(write_fails=True)
        middleware = make_middleware(backend)
        handler = OverflowThenSucceed()

        # The same failing backend also fails the history offload, which warns.
        with pytest.warns(UserWarning, match="failed during summarization"):
            result = middleware.wrap_model_call(make_request(make_overflow_conversation()), handler)

        assert isinstance(result, ExtendedModelResponse)
        assert result.command is not None
        assert result.command.update is not None
        assert "messages" not in result.command.update
        assert handler.payloads[1][-1].content == HUGE_TOOL_RESULT


class TestClipHelpers:
    """Unit coverage for the `_overflow_clip` primitives."""

    def test_derive_threshold_from_tokens_keep(self) -> None:
        assert _derive_overflow_clip_threshold_tokens(("tokens", 1234), None) == 1234

    def test_derive_threshold_from_fraction_keep(self) -> None:
        assert _derive_overflow_clip_threshold_tokens(("fraction", 0.1), 100_000) == 10_000

    def test_derive_threshold_falls_back_without_token_info(self) -> None:
        assert _derive_overflow_clip_threshold_tokens(("messages", 20), 100_000) == DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS
        assert _derive_overflow_clip_threshold_tokens(("fraction", 0.1), None) == DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS

    def test_find_tail_batch_requires_trailing_tool_messages(self) -> None:
        assert _find_tail_tool_message_batch([]) is None
        assert _find_tail_tool_message_batch([HumanMessage(content="x")]) is None

    def test_find_tail_batch_spans_consecutive_tool_messages(self) -> None:
        messages: list[AnyMessage] = [
            HumanMessage(content="x"),
            AIMessage(content="y"),
            ToolMessage(content="a", tool_call_id="t1"),
            ToolMessage(content="b", tool_call_id="t2"),
        ]
        found = _find_tail_tool_message_batch(messages)
        assert found is not None
        start, batch = found
        assert start == 2
        assert [m.tool_call_id for m in batch] == ["t1", "t2"]

    def test_build_tool_call_index(self) -> None:
        messages: list[AnyMessage] = [
            AIMessage(content="", tool_calls=[{"id": "t1", "name": "read_file", "args": {"file_path": "/a"}}]),
            ToolMessage(content="body", tool_call_id="t1"),
        ]
        index = _build_tool_call_index(messages)
        assert index["t1"]["name"] == "read_file"

    def test_read_file_original_path_ignores_other_tools(self) -> None:
        index = {"t1": {"id": "t1", "name": "grep", "args": {"file_path": "/a"}}}
        assert _read_file_original_path(ToolMessage(content="x", tool_call_id="t1"), index) is None

    def test_read_file_original_path_requires_nonempty_path(self) -> None:
        index = {"t1": {"id": "t1", "name": "read_file", "args": {"file_path": ""}}}
        assert _read_file_original_path(ToolMessage(content="x", tool_call_id="t1"), index) is None

    def test_clip_preserves_message_count_and_order(self) -> None:
        """Street-sweeper invariant: only text changes, never count or order."""
        backend = RecordingBackend()
        preserved: list[AnyMessage] = [
            AIMessage(content="", id="a1", tool_calls=[{"id": "t1", "name": "search", "args": {}}]),
            ToolMessage(content=HUGE_TOOL_RESULT, tool_call_id="t1", id="tm1"),
            ToolMessage(content=HUGE_TOOL_RESULT, tool_call_id="t2", id="tm2"),
        ]

        clipped, replacements = _clip_overflow_tail(
            preserved,
            backend,
            keep=("messages", 2),
            max_input_tokens=100_000,
            token_counter=count_tokens_approximately,
            large_tool_results_prefix="/large_tool_results",
        )

        assert len(clipped) == len(preserved)
        assert [m.id for m in clipped] == ["a1", "tm1", "tm2"]
        assert [m.id for m in replacements] == ["tm1", "tm2"]
        assert all(len(str(m.content)) < len(HUGE_TOOL_RESULT) for m in replacements)

    def test_clip_is_noop_for_non_tool_tail(self) -> None:
        backend = RecordingBackend()
        preserved: list[AnyMessage] = [HumanMessage(content="x" * 100_000, id="h1")]

        clipped, replacements = _clip_overflow_tail(
            preserved,
            backend,
            keep=("messages", 2),
            max_input_tokens=100_000,
            token_counter=count_tokens_approximately,
            large_tool_results_prefix="/large_tool_results",
        )

        assert clipped is preserved
        assert replacements == []
        assert backend.write_calls == []


class TestEvictionHelpers:
    """Unit coverage for the extracted `_message_eviction` helpers."""

    def test_evicted_tool_message_preserves_identity_fields(self) -> None:
        original = ToolMessage(
            content="body",
            tool_call_id="tc-9",
            id="m-9",
            name="search",
            status="error",
            additional_kwargs={"k": "v"},
        )
        replacement = _build_evicted_tool_message(original, "clipped")
        assert replacement.id == "m-9"
        assert replacement.tool_call_id == "tc-9"
        assert replacement.name == "search"
        assert replacement.status == "error"
        assert replacement.additional_kwargs == {"k": "v"}
        assert replacement.content == "clipped"

    def test_evicted_content_preserves_media_blocks(self) -> None:
        backend = RecordingBackend()
        original = ToolMessage(
            content=[
                {"type": "text", "text": HUGE_TOOL_RESULT},
                {"type": "image", "url": "https://example.com/a.png"},
            ],
            tool_call_id="tc-3",
            id="m-3",
        )

        clipped, _ = _clip_overflow_tail(
            [original],
            backend,
            keep=("messages", 2),
            max_input_tokens=100_000,
            token_counter=count_tokens_approximately,
            large_tool_results_prefix="/large_tool_results",
        )

        blocks = clipped[0].content
        assert isinstance(blocks, list)
        assert any(isinstance(b, dict) and b.get("type") == "image" for b in blocks)
        assert TOO_LARGE_TOOL_MSG.split("{")[0] in _extract_text_from_message(cast("ToolMessage", clipped[0]))

    def test_offload_path_sanitizes_tool_call_id(self) -> None:
        backend = RecordingBackend()
        original = ToolMessage(content=HUGE_TOOL_RESULT, tool_call_id="../../etc/passwd", id="m-4")

        _clip_overflow_tail(
            [original],
            backend,
            keep=("messages", 2),
            max_input_tokens=100_000,
            token_counter=count_tokens_approximately,
            large_tool_results_prefix="/large_tool_results",
        )

        assert backend.write_calls[0][0] == "/large_tool_results/______etc_passwd"
