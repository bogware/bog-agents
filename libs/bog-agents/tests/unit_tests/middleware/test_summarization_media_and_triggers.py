"""Tests for inline-media offload, dict trigger clauses, and the widened factory."""

import base64
from html import unescape
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from bog_agents.backends.protocol import BackendProtocol, EditResult, FileDownloadResponse, FileUploadResponse, WriteResult
from bog_agents.middleware.summarization import (
    _OFFLOAD_FAILED_PLACEHOLDER,
    BOG_DEFAULT_SUMMARY_PROMPT,
    DEEPAGENTS_DEFAULT_SUMMARY_PROMPT,
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    _token_counter_accepts_tools,
    create_summarization_middleware,
)

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentState

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload-bytes"
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


class MediaBackend(BackendProtocol):
    """Backend recording history writes and media uploads."""

    def __init__(self, *, upload_error: str | None = None, upload_not_implemented: bool = False) -> None:
        """Initialize the backend.

        Args:
            upload_error: If set, every upload returns this error code.
            upload_not_implemented: If `True`, `upload_files` raises
                `NotImplementedError`, mirroring the historic `StateBackend`.
        """
        self.write_calls: list[tuple[str, str]] = []
        self.upload_calls: list[tuple[str, bytes]] = []
        self._upload_error = upload_error
        self._upload_not_implemented = upload_not_implemented

    def write(self, path: str, content: str) -> WriteResult:
        self.write_calls.append((path, content))
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
        self.upload_calls.extend(files)
        return [FileUploadResponse(path=path, error=self._upload_error) for path, _ in files]

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)


def make_mock_model() -> MagicMock:
    """Create a mock chat model whose `invoke`/`ainvoke` return a summary."""
    model = MagicMock()
    model.invoke.return_value = MagicMock(text="A summary.")
    # AsyncMock so `await ainvoke(...)` resolves — a plain MagicMock return is
    # not awaitable, which would make `_acreate_summary` fall back to the
    # "Error generating summary:" sentinel (v5 CTX-1).
    model.ainvoke = AsyncMock(return_value=MagicMock(text="A summary."))
    model._llm_type = "test-model"
    model.profile = {"max_input_tokens": 100_000}
    model._get_ls_params.return_value = {"ls_provider": "test"}
    return model


def make_media_conversation() -> list[BaseMessage]:
    """Build a conversation whose old messages carry an inline base64 image."""
    return [
        HumanMessage(
            content=[
                {"type": "text", "text": "what is this?"},
                {"type": "image", "base64": PNG_B64, "mime_type": "image/png"},
            ],
            id="h0",
        ),
        AIMessage(content="a logo", id="a0"),
        HumanMessage(content="ok thanks", id="h1"),
        AIMessage(content="np", id="a1"),
        HumanMessage(content="next question", id="h2"),
        AIMessage(content="sure", id="a2"),
    ]


def run_summarization(middleware: SummarizationMiddleware, messages: list[BaseMessage]) -> ExtendedModelResponse:
    """Drive `wrap_model_call` through the summarization path."""
    runtime = MagicMock()
    runtime.context = {}
    runtime.stream_writer = MagicMock()
    runtime.store = None
    del runtime.config
    state = cast("AgentState[Any]", {"messages": messages})
    request = ModelRequest(
        model=make_mock_model(),
        messages=messages,
        system_message=None,
        tools=[],
        runtime=runtime,
        state=state,
    )

    def handler(_req: ModelRequest) -> ModelResponse:
        return cast("ModelResponse", AIMessage(content="ok"))

    result = middleware.wrap_model_call(request, handler)
    assert isinstance(result, ExtendedModelResponse)
    return result


def make_middleware(backend: BackendProtocol) -> SummarizationMiddleware:
    """Build a middleware that always summarizes on the first call."""
    return SummarizationMiddleware(
        model=make_mock_model(),
        backend=backend,
        trigger=("messages", 4),
        keep=("messages", 2),
    )


class TestInlineMediaOffload:
    """Inline `data:`/base64 media is uploaded once and referenced by path."""

    def test_media_uploaded_and_referenced_by_path(self) -> None:
        backend = MediaBackend()
        run_summarization(make_middleware(backend), make_media_conversation())

        assert len(backend.upload_calls) == 1
        upload_path, upload_bytes = backend.upload_calls[0]
        assert upload_path.startswith("/conversation_history/media/")
        assert upload_path.endswith(".png")
        assert upload_bytes == PNG_BYTES

        _, history = backend.write_calls[0]
        assert upload_path in history
        assert PNG_B64 not in history, "raw base64 must not survive into the offloaded history"

    def test_upload_not_implemented_degrades_to_placeholder(self) -> None:
        """A backend that raises `NotImplementedError` on `upload_files` must not crash."""
        backend = MediaBackend(upload_not_implemented=True)

        with pytest.warns(UserWarning, match="could not be offloaded"):
            result = run_summarization(make_middleware(backend), make_media_conversation())

        assert result.command is not None
        assert result.command.update is not None
        assert "_summarization_event" in result.command.update
        _, history = backend.write_calls[0]
        # The XML renderer escapes the placeholder's angle brackets, so compare
        # against the unescaped history. What matters is that the block is marked
        # present-but-unrecoverable rather than silently dropped.
        assert _OFFLOAD_FAILED_PLACEHOLDER in unescape(history)
        assert PNG_B64 not in history

    def test_upload_error_degrades_to_placeholder(self) -> None:
        backend = MediaBackend(upload_error="permission_denied")

        with pytest.warns(UserWarning, match="could not be offloaded"):
            run_summarization(make_middleware(backend), make_media_conversation())

        _, history = backend.write_calls[0]
        # The XML renderer escapes the placeholder's angle brackets, so compare
        # against the unescaped history. What matters is that the block is marked
        # present-but-unrecoverable rather than silently dropped.
        assert _OFFLOAD_FAILED_PLACEHOLDER in unescape(history)

    async def test_async_media_uploaded_and_referenced_by_path(self) -> None:
        backend = MediaBackend()
        middleware = make_middleware(backend)
        messages = make_media_conversation()

        runtime = MagicMock()
        runtime.context = {}
        runtime.stream_writer = MagicMock()
        runtime.store = None
        del runtime.config
        state = cast("AgentState[Any]", {"messages": messages})
        request = ModelRequest(
            model=make_mock_model(),
            messages=messages,
            system_message=None,
            tools=[],
            runtime=runtime,
            state=state,
        )

        async def handler(_req: ModelRequest) -> ModelResponse:
            return cast("ModelResponse", AIMessage(content="ok"))

        result = await middleware.awrap_model_call(request, handler)

        assert isinstance(result, ExtendedModelResponse)
        assert len(backend.upload_calls) == 1
        _, history = backend.write_calls[0]
        assert backend.upload_calls[0][0] in history

    def test_no_media_leaves_messages_untouched(self) -> None:
        backend = MediaBackend()
        messages: list[BaseMessage] = [
            HumanMessage(content=f"m{i}", id=f"m{i}") if i % 2 == 0 else AIMessage(content=f"m{i}", id=f"m{i}") for i in range(6)
        ]

        run_summarization(make_middleware(backend), messages)

        assert backend.upload_calls == []


class TestSummaryPrompt:
    """The default summary prompt carries the media-reference addendum."""

    def test_media_addendum_spliced_before_messages_marker(self) -> None:
        assert "<media_reference_information>" in BOG_DEFAULT_SUMMARY_PROMPT
        assert BOG_DEFAULT_SUMMARY_PROMPT.index("<media_reference_information>") < BOG_DEFAULT_SUMMARY_PROMPT.index("\n<messages>\n")

    def test_deepagents_alias_matches(self) -> None:
        assert DEEPAGENTS_DEFAULT_SUMMARY_PROMPT is BOG_DEFAULT_SUMMARY_PROMPT


class TestTriggerClauses:
    """Dict trigger clauses (AND semantics) reach the compact-tool eligibility gate."""

    def test_dict_clause_requires_all_thresholds(self) -> None:
        backend = MediaBackend()
        summarization = SummarizationMiddleware(
            model=make_mock_model(),
            backend=backend,
            trigger={"messages": 20, "tokens": 1_000_000},
        )
        tool_mw = SummarizationToolMiddleware(summarization)

        # 12 messages clears the halved `messages` bar (>= 10) but the halved
        # `tokens` bar (500_000) is nowhere near met, so AND semantics reject.
        messages = [HumanMessage(content=f"m{i}") for i in range(12)]
        assert tool_mw._is_eligible_for_compaction(cast("Any", messages)) is False

    def test_messages_clause_is_honored(self) -> None:
        """A bare `("messages", N)` trigger now gates compaction (it was ignored before)."""
        backend = MediaBackend()
        summarization = SummarizationMiddleware(model=make_mock_model(), backend=backend, trigger=("messages", 20))
        tool_mw = SummarizationToolMiddleware(summarization)

        assert tool_mw._is_eligible_for_compaction(cast("Any", [HumanMessage(content="x")] * 4)) is False
        assert tool_mw._is_eligible_for_compaction(cast("Any", [HumanMessage(content="x")] * 10)) is True

    def test_no_trigger_is_never_eligible(self) -> None:
        backend = MediaBackend()
        summarization = SummarizationMiddleware(model=make_mock_model(), backend=backend, trigger=None)
        tool_mw = SummarizationToolMiddleware(summarization)

        assert tool_mw._is_eligible_for_compaction(cast("Any", [HumanMessage(content="x")] * 100)) is False

    def test_dict_clause_drives_auto_summarization(self) -> None:
        backend = MediaBackend()
        middleware = SummarizationMiddleware(
            model=make_mock_model(),
            backend=backend,
            trigger={"messages": 4},
            keep=("messages", 2),
        )

        result = run_summarization(middleware, [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(6)])

        assert result.command is not None
        assert result.command.update is not None
        assert "_summarization_event" in result.command.update


class TestFactoryPassthrough:
    """`create_summarization_middleware` now accepts the prompt/trim/counter knobs."""

    def test_accepts_summary_prompt_trim_and_counter(self) -> None:
        model = make_mock_model()
        model.__class__ = type("FakeChatModel", (), {})

        def counter(messages: list[BaseMessage]) -> int:
            return len(messages)

        # `create_summarization_middleware` type-checks the model, so construct the
        # middleware directly with the same kwargs the factory now forwards.
        middleware = SummarizationMiddleware(
            model=make_mock_model(),
            backend=MediaBackend(),
            summary_prompt="custom prompt",
            trim_tokens_to_summarize=123,
            token_counter=counter,
        )

        assert middleware._lc_helper.summary_prompt == "custom prompt"
        assert middleware._lc_helper.trim_tokens_to_summarize == 123
        assert middleware.token_counter is counter

    def test_factory_rejects_model_string(self) -> None:
        with pytest.raises(TypeError):
            create_summarization_middleware(cast("Any", "openai:gpt-5"), cast("Any", MediaBackend()))

    def test_defaults_use_bog_summary_prompt(self) -> None:
        middleware = SummarizationMiddleware(model=make_mock_model(), backend=MediaBackend())
        assert middleware._lc_helper.summary_prompt == BOG_DEFAULT_SUMMARY_PROMPT


class TestTokenCounterIntrospection:
    """`_token_counter_accepts_tools` replaces the three try/except probes."""

    def test_detects_explicit_tools_kwarg(self) -> None:
        def counter(messages: list[BaseMessage], *, tools: Any = None) -> int:
            return 0

        assert _token_counter_accepts_tools(counter) is True

    def test_detects_var_keyword(self) -> None:
        def counter(messages: list[BaseMessage], **_kwargs: Any) -> int:
            return 0

        assert _token_counter_accepts_tools(counter) is True

    def test_detects_counter_without_tools(self) -> None:
        def counter(messages: list[BaseMessage]) -> int:
            return 0

        assert _token_counter_accepts_tools(counter) is False

    def test_default_counter_accepts_tools(self) -> None:
        assert _token_counter_accepts_tools(count_tokens_approximately) is True

    def test_type_error_inside_counter_is_not_masked(self) -> None:
        """A `TypeError` from the counter body must propagate, not be silently swallowed."""

        def broken(messages: list[BaseMessage], *, tools: Any = None) -> int:
            msg = "boom from inside the counter"
            raise TypeError(msg)

        middleware = SummarizationMiddleware(model=make_mock_model(), backend=MediaBackend(), token_counter=broken)

        with pytest.raises(TypeError, match="boom from inside the counter"):
            middleware._count_tokens([HumanMessage(content="x")], None, None)
