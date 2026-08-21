"""Summarization middleware for automatic and tool-based conversation compaction.

This module provides two middleware classes and a convenience factory:

- `SummarizationMiddleware` — automatically compacts the conversation when token
    usage exceeds a configurable threshold.

    Older messages are summarized via an LLM call and the full history is
    offloaded to a backend for later retrieval.
- `SummarizationToolMiddleware` — exposes a `compact_conversation` tool that
    lets the agent (or a human-in-the-loop approval flow) trigger compaction on
    demand.

    Composes with a `SummarizationMiddleware` instance and reuses its
    summarization engine.
- `create_summarization_tool_middleware` — convenience factory that creates both
    middleware layers with model-aware defaults.

## Usage

```python
from bog_agents import create_agent
from bog_agents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
)
from bog_agents.backends import FilesystemBackend

backend = FilesystemBackend(root_dir="/data")

summ = SummarizationMiddleware(
    model="gpt-4o-mini",
    backend=backend,
    trigger=("fraction", 0.85),
    keep=("fraction", 0.10),
)
tool_mw = SummarizationToolMiddleware(summ)

agent = create_agent(middleware=[summ, tool_mw])
```

## Storage

Offloaded messages are stored as markdown at `/conversation_history/{thread_id}.md`.

Each summarization event appends a new section to this file, creating a running
log of all evicted messages. Base64 (and other inline `data:`) media in evicted
messages is written separately under `<history_prefix>/media/` and referenced by
path from the markdown, so the history file stays text-only (see
`_offload_inline_media`).

## Summary prompt

`BOG_DEFAULT_SUMMARY_PROMPT` augments LangChain's `DEFAULT_SUMMARY_PROMPT` with
an addendum explaining the media reference tags that the offloading behavior
introduces, so the summarizing model knows to preserve them. It is the default
`summary_prompt` for `SummarizationMiddleware` and both factories.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
import mimetypes
import urllib.parse
import uuid
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, cast

from langchain.agents.middleware.summarization import (
    DEFAULT_SUMMARY_PROMPT,
    ContextSize,
    SummarizationMiddleware as LCSummarizationMiddleware,
    TokenCounter,
)
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ExtendedModelResponse, PrivateStateAttr
from langchain.tools import ToolRuntime
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage, get_buffer_string
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.config import get_config
from langgraph.types import Command
from typing_extensions import TypedDict

from bog_agents.middleware._context_errors import is_context_length_error
from bog_agents.middleware._overflow_clip import _aclip_overflow_tail, _clip_overflow_tail
from bog_agents.middleware._utils import append_to_system_message

# Inlined from langchain.agents.middleware.summarization to avoid coupling
# bog-agents to a private langchain symbol (leading underscore). The
# upstream constants matched these values at langchain 1.2.x; if upstream
# changes them we want our defaults to remain stable rather than break
# on a langchain minor bump. See P0-J in REVIEW.md.
_DEFAULT_MESSAGES_TO_KEEP = 20
_DEFAULT_TRIM_TOKEN_LIMIT = 4000

DEFAULT_LARGE_TOOL_RESULTS_PREFIX = "/large_tool_results"
"""Default backend prefix for tool results offloaded by the overflow clip."""

_MEDIA_REFERENCE_SUMMARY_PROMPT = """<media_reference_information>
Conversation history may include XML media reference tags, for example:
<image url=\"/conversation_history/media/{{hash}}.png\" />
These tags mean the original message included media that was preserved at the referenced backend path.
Treat the tag and path as part of the conversation context. Do not infer visual details that are not available from surrounding text.
When the media could be important for future context, preserve the media reference in your summary.
The model consuming the summary can call `read_file` on the referenced path if it needs to inspect the media.
</media_reference_information>"""

# NOTE: This splices the media-reference addendum in just before the
# `<messages>` marker that `DEFAULT_SUMMARY_PROMPT` exposes. That marker is a
# load-bearing contract -- see the `DEFAULT_SUMMARY_PROMPT` docstring in
# langchain for the downstream-dependency note.
BOG_DEFAULT_SUMMARY_PROMPT = DEFAULT_SUMMARY_PROMPT.replace(
    "\n<messages>\n",
    f"\n{_MEDIA_REFERENCE_SUMMARY_PROMPT}\n\n<messages>\n",
    1,
)
"""LangChain's `DEFAULT_SUMMARY_PROMPT` plus the media-reference addendum."""

DEEPAGENTS_DEFAULT_SUMMARY_PROMPT = BOG_DEFAULT_SUMMARY_PROMPT
"""Upstream-compatible alias for `BOG_DEFAULT_SUMMARY_PROMPT`."""

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain.chat_models import BaseChatModel
    from langchain_core.runnables.config import RunnableConfig
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime

    from bog_agents.backends.protocol import BACKEND_TYPES, BackendProtocol, FileUploadResponse

logger = logging.getLogger(__name__)

# LangChain's `_create_summary` swallows a failed summary-model call into this
# sentinel string instead of raising (its summarization.py:822/848). Committing
# that string as the permanent conversation summary silently evicts all
# pre-cutoff context on the exact failure the overflow-heal path claims to fix
# (v5 CTX-1) — so every commit site checks for it first.
_SUMMARY_ERROR_SENTINEL = "Error generating summary:"


def _summary_failed(summary: str) -> bool:
    """Whether `summary` is LangChain's swallowed-error sentinel, not a real summary."""
    return summary.lstrip().startswith(_SUMMARY_ERROR_SENTINEL)


SUMMARIZATION_SYSTEM_PROMPT = """## Compact conversation Tool `compact_conversation`

You have access to a `compact_conversation` tool. This tool refreshes your context window to reduce context bloat and costs.

You should use the tool when:
- The user asks to move on to a completely new task for which previous context is likely irrelevant.
- You have finished extracting or synthesizing a result and previous working context is no longer needed.
"""


__all__ = [
    "BOG_DEFAULT_SUMMARY_PROMPT",
    "DEEPAGENTS_DEFAULT_SUMMARY_PROMPT",
    "SummarizationDefaults",
    "SummarizationEvent",
    "SummarizationState",
    "SummarizationToolMiddleware",
    "TriggerClause",
    "TruncateArgsSettings",
]


class TriggerClause(TypedDict, total=False):
    """Dictionary-based summarization trigger with AND semantics.

    Attributes:
        tokens: Trigger when token count reaches or exceeds this value.
        messages: Trigger when message count reaches or exceeds this value.
        fraction: Trigger when token count reaches this fraction of the model
            context window.
    """

    tokens: int
    messages: int
    fraction: float


class SummarizationEvent(TypedDict):
    """Represents a summarization event.

    Attributes:
        cutoff_index: The index in the messages list where summarization occurred.
        summary_message: The HumanMessage containing the summary.
        file_path: Path where the conversation history was offloaded, or None if offload failed.
    """

    cutoff_index: int
    summary_message: HumanMessage
    file_path: str | None


class TruncateArgsSettings(TypedDict, total=False):
    """Settings for truncating large tool-call arguments in older messages.

    This is a lightweight, pre-summarization optimization that fires at a lower
    token threshold than full conversation compaction. When triggered, only the
    `args` values on `AIMessage.tool_calls` in messages *before* the keep window
    are shortened — recent messages are left intact.

    Typical large arguments include `write_file` content, `edit_file` patches,
    and verbose `execute` outputs.

    Args:
        trigger: Token/message/fraction threshold that activates truncation.

            Uses the same `ContextSize` format as the summarization trigger.

            If `None`, truncation is disabled.
        keep: How many recent messages (or tokens/fraction of context) to
            leave untouched.
        max_length: Character limit per argument value before it is clipped.
        truncation_text: Replacement suffix appended after the first 20
            characters of a truncated argument.
    """

    trigger: ContextSize | None
    keep: ContextSize
    max_length: int
    truncation_text: str


class SummarizationState(AgentState):
    """State for the summarization middleware.

    Extends AgentState with a private field for tracking summarization events.
    """

    _summarization_event: Annotated[NotRequired[SummarizationEvent | None], PrivateStateAttr]
    """Private field storing the most recent summarization event."""


class SummarizationDefaults(TypedDict):
    """Default settings computed from model profile."""

    trigger: ContextSize
    keep: ContextSize
    truncate_args_settings: TruncateArgsSettings


def compute_summarization_defaults(model: BaseChatModel) -> SummarizationDefaults:
    """Compute default summarization settings based on model profile.

    Args:
        model: A resolved chat model instance.

    Returns:
        Default settings for trigger, keep, and truncate_args_settings.
            If the model has a profile with `max_input_tokens`, uses
            fraction-based settings. Otherwise, uses fixed token/message counts.
    """
    has_profile = (
        model.profile is not None
        and isinstance(model.profile, dict)
        and "max_input_tokens" in model.profile
        and isinstance(model.profile["max_input_tokens"], int)
    )

    if has_profile:
        return {
            "trigger": ("fraction", 0.85),
            "keep": ("fraction", 0.10),
            "truncate_args_settings": {
                "trigger": ("fraction", 0.85),
                "keep": ("fraction", 0.10),
            },
        }

    # Defaults for models without profile info are more conservative to avoid
    # overshooting context limits.
    return {
        "trigger": ("tokens", 170000),
        "keep": ("messages", 6),
        "truncate_args_settings": {
            "trigger": ("messages", 20),
            "keep": ("messages", 20),
        },
    }


def _token_counter_accepts_tools(counter: TokenCounter) -> bool | None:
    """Determine whether `counter` accepts a `tools` keyword argument.

    The `TokenCounter` contract only requires accepting messages, but the default
    counter (and most modern ones) also accept `tools=` so tool schemas contribute
    to the count. Rather than probe by calling and catching `TypeError` — which
    cannot distinguish a signature that rejects `tools` from a genuine `TypeError`
    raised inside the counter's body — the signature is inspected directly.

    Args:
        counter: The token-counting callable to inspect.

    Returns:
        `True` if the signature declares a `tools` parameter or accepts arbitrary
            keyword arguments (`**kwargs`), `False` if it clearly does not, or
            `None` when the signature cannot be introspected (some C-level
            callables expose no signature), signaling that callers should fall
            back to probing.
    """
    try:
        parameters = inspect.signature(counter).parameters
    except (TypeError, ValueError):
        return None
    for param in parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "tools" and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


_OFFLOAD_FAILED_PLACEHOLDER = '<image error="failed_to_offload" />'
"""Text placeholder written when a media block cannot be offloaded.

Marks the spot so the saved history shows a block was present rather than
silently omitting it.
"""


def _is_data_url(url: str) -> bool:
    """Return whether `url` is an inline `data:` URL.

    Any `data:` URL is treated as inline media to offload, because the XML history
    renderer drops `data:` URL blocks entirely (only `http(s)`-style references
    survive). This covers both base64 (`data:<mime>;base64,<payload>`) and
    percent-encoded / plaintext (`data:<mime>,<payload>`, e.g. an inline SVG)
    forms; whether the payload actually decodes is left to `_decode_data_url`.

    Args:
        url: The candidate URL.

    Returns:
        Whether the URL is a `data:` URL.
    """
    return url.startswith("data:")


def _extract_data_url(block: Any) -> str | None:
    """Return the embedded `data:` URL for an inline-media content block.

    Detects the three inline-data content-block shapes that appear across LangChain
    messages:

    1. A standard content block with an explicit `base64` field.
    2. A `data:` URL on the `url` field.
    3. An OpenAI-style `image_url` block whose `url` is a `data:` URL.

    This is pure detection and never raises: it reports *whether* a block carries
    inline data, leaving decoding (which can fail) to `_decode_data_url`.

    Args:
        block: A single content block (usually a dict).

    Returns:
        The block's `data:` URL, or `None` if the block carries no inline data.
    """
    if not isinstance(block, dict):
        return None

    # 1. Standard content block with an explicit base64 field.
    raw_b64 = block.get("base64")
    if raw_b64:
        mime = block.get("mime_type") or "application/octet-stream"
        return f"data:{mime};base64,{raw_b64}"

    # 2. Top-level data: URL.
    url = block.get("url", "")
    if isinstance(url, str) and _is_data_url(url):
        return url

    # 3. OpenAI-style image_url with a data: URL.
    image_url = block.get("image_url")
    if isinstance(image_url, dict):
        inner = image_url.get("url", "")
        if isinstance(inner, str) and _is_data_url(inner):
            return inner

    return None


def _decode_data_url(data_url: str) -> tuple[bytes, str, str] | None:
    """Decode a `data:` URL to raw bytes, a file extension, and a MIME type.

    Handles both encodings a `data:` URL can use: a `;base64,` payload is
    base64-decoded, while a plain `data:<mime>,<payload>` payload is treated as
    percent-encoded text (e.g. an inline SVG).

    Args:
        data_url: A `data:<mime>[;base64],<payload>` URL.

    Returns:
        A `(raw_bytes, extension, mime_type)` tuple, or `None` if decoding fails
            (including a malformed URL with no `,` payload separator).
    """
    try:
        header, payload = data_url.split(",", 1)
        mime = header.split(":")[1].split(";")[0] if ":" in header else "application/octet-stream"
        ext = (mimetypes.guess_extension(mime) or ".bin").lstrip(".")
        is_base64 = "base64" in header.lower().split(";")
        raw = base64.b64decode(payload) if is_base64 else urllib.parse.unquote_to_bytes(payload)
    except Exception as e:
        logger.warning("Failed to decode data: content block (%s): %s", type(e).__name__, e)
        return None
    else:
        return raw, ext, mime


def _media_reference_block(path: str, mime: str) -> dict[str, Any]:
    """Build a content block referencing offloaded media by backend path.

    The block type is chosen so the XML history renderer serializes the reference:
    `image`, `audio`, and `video` map to their typed blocks, while any other MIME
    type falls back to a text block (the renderer has no generic file block and
    would otherwise drop it).

    Args:
        path: Backend path where the media was stored.
        mime: MIME type of the original media, used to pick the block type.

    Returns:
        A content block carrying the path reference.
    """
    major = mime.split("/", 1)[0]
    if major in {"image", "audio", "video"}:
        return {"type": major, "url": path}
    return {"type": "text", "text": f'<file url="{path}" />'}


def _rewrite_data_url_blocks(
    messages: list[AnyMessage],
    path_map: dict[str, str],
) -> tuple[list[AnyMessage], int]:
    """Rewrite inline `data:` URL blocks using uploaded media paths.

    Each inline-data block whose content hash appears in `path_map` becomes a typed
    media reference block. Blocks whose upload failed — or whose payload could not
    be decoded — become an `<image error="failed_to_offload" />` text placeholder so
    the saved history records that media was present rather than silently dropping
    it. Blocks without inline data pass through unchanged.

    Args:
        messages: Messages whose inline-data blocks should be rewritten.
        path_map: Mapping of `sha256[:16]` to backend paths for uploaded media.

    Returns:
        A `(messages, failed_block_count)` tuple. `failed_block_count` is the number
            of blocks rewritten to a failed-offload placeholder. Message count and
            order are never changed.
    """
    rewritten: list[AnyMessage] = []
    failed_blocks = 0
    for msg in messages:
        new_blocks: list[Any] = []
        modified = False
        for block in msg.content_blocks:
            data_url = _extract_data_url(block)
            if data_url is None:
                new_blocks.append(block)
                continue
            modified = True
            decoded = _decode_data_url(data_url)
            if decoded is not None:
                raw, _ext, mime = decoded
                key = hashlib.sha256(raw).hexdigest()[:16]
                if key in path_map:
                    new_blocks.append(_media_reference_block(path_map[key], mime))
                    continue
            failed_blocks += 1
            new_blocks.append({"type": "text", "text": _OFFLOAD_FAILED_PLACEHOLDER})
        if modified:
            new_msg = msg.model_copy()
            new_msg.content = new_blocks
            rewritten.append(new_msg)
        else:
            rewritten.append(msg)
    return rewritten, failed_blocks


def _upload_response_error(responses: list[FileUploadResponse]) -> str | None:
    """Extract an error from a single-file batch upload result.

    Args:
        responses: Backend upload responses. `upload_files`/`aupload_files` are
            batch APIs that return one response per input file in order. Media
            offloading passes exactly one file at a time.

    Returns:
        The upload error, `'missing_upload_response'` if the backend returned no
            response, or `None` when the upload succeeded.
    """
    if not responses:
        return "missing_upload_response"
    error = responses[0].error
    if error is None:
        return None
    return str(error)


class _BogAgentsSummarizationMiddleware(AgentMiddleware):
    """Summarization middleware with backend for conversation history offloading."""

    state_schema = SummarizationState

    def __init__(
        self,
        model: str | BaseChatModel,
        *,
        backend: BACKEND_TYPES,
        trigger: ContextSize | TriggerClause | list[ContextSize | TriggerClause] | None = None,
        keep: ContextSize = ("messages", _DEFAULT_MESSAGES_TO_KEEP),
        token_counter: TokenCounter = count_tokens_approximately,
        summary_prompt: str = BOG_DEFAULT_SUMMARY_PROMPT,
        trim_tokens_to_summarize: int | None = _DEFAULT_TRIM_TOKEN_LIMIT,
        history_path_prefix: str = "/conversation_history",
        truncate_args_settings: TruncateArgsSettings | None = None,
        large_tool_results_prefix: str | None = None,
        **deprecated_kwargs: Any,
    ) -> None:
        """Initialize summarization middleware with backend support.

        Args:
            model: The language model to use for generating summaries.
            backend: Backend instance or factory for persisting conversation history.
            trigger: Threshold(s) that trigger summarization. A tuple is a single
                threshold, a [`TriggerClause`][bog_agents.middleware.summarization.TriggerClause]
                dict combines thresholds with AND semantics, and a list combines
                items with OR semantics.
            keep: Context retention policy after summarization.

                Defaults to keeping last 20 messages.
            token_counter: Function to count tokens in messages.
            summary_prompt: Prompt template for generating summaries.
            trim_tokens_to_summarize: Max tokens to include when generating summary.

                Defaults to 4000.
            truncate_args_settings: Settings for truncating large tool arguments in old messages.

                Provide a [`TruncateArgsSettings`][bog_agents.middleware.summarization.TruncateArgsSettings]
                dictionary to configure when and how to truncate tool arguments. If `None`,
                argument truncation is disabled.

                !!! example

                    ```python
                    # Truncate when 50 messages is reached, ignoring the last 20 messages
                    {"trigger": ("messages", 50), "keep": ("messages", 20), "max_length": 2000, "truncation_text": "...(truncated)"}

                    # Truncate when 50% of context window reached, ignoring messages in last 10% of window
                    {"trigger": ("fraction", 0.5), "keep": ("fraction", 0.1), "max_length": 2000, "truncation_text": "...(truncated)"}
            history_path_prefix: Path prefix for storing conversation history.
            large_tool_results_prefix: Path prefix for tool results offloaded by the
                `ContextOverflowError` tail clip. When `None`, a `CompositeBackend`'s
                `artifacts_root` is used if available, otherwise
                `/large_tool_results`.

        Example:
            ```python
            from bog_agents.middleware.summarization import SummarizationMiddleware
            from bog_agents.backends import StateBackend

            middleware = SummarizationMiddleware(
                model="gpt-4o-mini",
                backend=lambda tool_runtime: StateBackend(tool_runtime),
                trigger=("tokens", 100000),
                keep=("messages", 20),
            )
            ```
        """
        # Initialize langchain helper for core summarization logic
        self._lc_helper = LCSummarizationMiddleware(
            model=model,
            trigger=trigger,
            keep=keep,
            token_counter=token_counter,
            summary_prompt=summary_prompt,
            trim_tokens_to_summarize=trim_tokens_to_summarize,
            **deprecated_kwargs,
        )

        # Whether the configured token counter accepts a `tools` kwarg. Resolved
        # once here (the counter is fixed after construction) so the per-call
        # token count never pays signature-introspection cost. `None` means the
        # signature could not be introspected, so `_count_tokens` probes instead.
        self._counter_accepts_tools = _token_counter_accepts_tools(self.token_counter)

        # Bog Agents specific attributes
        self._backend = backend
        self._history_path_prefix = history_path_prefix
        self._media_prefix = f"{history_path_prefix.rstrip('/')}/media"
        self._large_tool_results_prefix = self._resolve_large_tool_results_prefix(backend, large_tool_results_prefix)

        # Parse truncate_args_settings
        if truncate_args_settings is None:
            self._truncate_args_trigger = None
            self._truncate_args_keep: ContextSize = ("messages", 20)
            self._max_arg_length = 2000
            self._truncation_text = "...(argument truncated)"
        else:
            self._truncate_args_trigger = truncate_args_settings.get("trigger")
            self._truncate_args_keep = truncate_args_settings.get("keep", ("messages", 20))
            self._max_arg_length = truncate_args_settings.get("max_length", 2000)
            self._truncation_text = truncate_args_settings.get("truncation_text", "...(argument truncated)")

    @staticmethod
    def _resolve_large_tool_results_prefix(backend: BACKEND_TYPES, override: str | None) -> str:
        """Resolve the backend prefix used for overflow-clipped tool results.

        Args:
            backend: Backend instance or factory passed to the constructor.
            override: Explicit prefix supplied by the caller, or `None`.

        Returns:
            The explicit override when given, otherwise a `CompositeBackend`'s
                `artifacts_root`, otherwise `DEFAULT_LARGE_TOOL_RESULTS_PREFIX`.
        """
        if override is not None:
            return override.rstrip("/") or "/"

        from bog_agents.backends.composite import CompositeBackend

        if isinstance(backend, CompositeBackend):
            return backend.artifacts_root.rstrip("/") or "/"
        return DEFAULT_LARGE_TOOL_RESULTS_PREFIX

    # Delegated properties and methods from langchain helper
    @property
    def model(self) -> BaseChatModel:
        """The language model used for generating summaries."""
        return self._lc_helper.model

    @property
    def token_counter(self) -> TokenCounter:
        """Function to count tokens in messages."""
        return self._lc_helper.token_counter

    def _get_profile_limits(self) -> int | None:
        """Retrieve max input token limit from the model profile."""
        return self._lc_helper._get_profile_limits()

    def _should_summarize(self, messages: list[AnyMessage], total_tokens: int) -> bool:
        """Determine whether summarization should run for the current token usage."""
        return self._lc_helper._should_summarize(messages, total_tokens)

    def _determine_cutoff_index(self, messages: list[AnyMessage]) -> int:
        """Choose cutoff index respecting retention configuration."""
        return self._lc_helper._determine_cutoff_index(messages)

    def _partition_messages(
        self,
        conversation_messages: list[AnyMessage],
        cutoff_index: int,
    ) -> tuple[list[AnyMessage], list[AnyMessage]]:
        """Partition messages into those to summarize and those to preserve."""
        return self._lc_helper._partition_messages(conversation_messages, cutoff_index)

    def _create_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """Generate summary for the given messages."""
        return self._lc_helper._create_summary(messages_to_summarize)

    async def _acreate_summary(self, messages_to_summarize: list[AnyMessage]) -> str:
        """Generate summary for the given messages (async)."""
        return await self._lc_helper._acreate_summary(messages_to_summarize)

    def _get_backend(
        self,
        state: AgentState[Any],
        runtime: Runtime,
    ) -> BackendProtocol:
        """Resolve backend from instance or factory.

        Args:
            state: Current agent state.
            runtime: Runtime context for factory functions.

        Returns:
            Resolved backend instance.
        """
        if callable(self._backend):
            # Because we're using `before_model`, which doesn't receive `config` as a
            # parameter, we access it via `runtime.config` instead.
            # Cast is safe: empty dict `{}` is a valid `RunnableConfig` (all fields are
            # optional in TypedDict).
            config = cast("RunnableConfig", getattr(runtime, "config", {}))

            tool_runtime = ToolRuntime(
                state=state,
                context=runtime.context,
                stream_writer=runtime.stream_writer,
                store=runtime.store,
                config=config,
                tool_call_id=None,
            )
            return self._backend(tool_runtime)  # ty: ignore[call-top-callable]
        return self._backend

    def _get_thread_id(self) -> str:
        """Extract `thread_id` from langgraph config.

        Uses `get_config()` to access the `RunnableConfig` from langgraph's
        `contextvar`. Falls back to a generated session ID if not available.

        Returns:
            Thread ID string from config, or a generated session ID
                (e.g., `'session_a1b2c3d4'`) if not in a runnable context.
        """
        try:
            config = get_config()
            thread_id = config.get("configurable", {}).get("thread_id")
            if thread_id is not None:
                return str(thread_id)
        except RuntimeError:
            # Not in a runnable context
            pass

        # Fallback: generate session ID
        generated_id = f"session_{uuid.uuid4().hex[:8]}"
        logger.debug("No thread_id found, using generated session ID: %s", generated_id)
        return generated_id

    def _get_history_path(self) -> str:
        """Generate path for storing conversation history.

        Returns a single file per thread that gets appended to over time.

        Returns:
            Path string like `'/conversation_history/{thread_id}.md'`
        """
        thread_id = self._get_thread_id()
        return f"{self._history_path_prefix}/{thread_id}.md"

    def _is_summary_message(self, msg: AnyMessage) -> bool:
        """Check if a message is a previous summarization message.

        Summary messages are `HumanMessage` objects with `lc_source='summarization'` in
        `additional_kwargs`. These should be filtered from offloads to avoid redundant
        storage during chained summarization.

        Args:
            msg: Message to check.

        Returns:
            Whether this is a summary `HumanMessage` from a previous summarization.
        """
        if not isinstance(msg, HumanMessage):
            return False
        return msg.additional_kwargs.get("lc_source") == "summarization"

    def _filter_summary_messages(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        """Filter out previous summary messages from a message list.

        When chained summarization occurs, we don't want to re-offload the previous
        summary `HumanMessage` since the original messages are already stored in the
        backend.

        Args:
            messages: List of messages to filter.

        Returns:
            Messages without previous summary `HumanMessage` objects.
        """
        return [msg for msg in messages if not self._is_summary_message(msg)]

    def _build_new_messages_with_path(self, summary: str, file_path: str | None) -> list[AnyMessage]:
        """Build the summary message with optional file path reference.

        Args:
            summary: The generated summary text.
            file_path: Path where conversation history was stored, or `None`.

                Optional since offloading may fail.

        Returns:
            List containing the summary `HumanMessage`.
        """
        if file_path is not None:
            content = f"""\
You are in the middle of a conversation that has been summarized.

The full conversation history has been saved to {file_path} should you need to refer back to it for details.

A condensed summary follows:

<summary>
{summary}
</summary>"""
        else:
            content = f"Here is a summary of the conversation to date:\n\n{summary}"

        return [
            HumanMessage(
                content=content,
                additional_kwargs={"lc_source": "summarization"},
            )
        ]

    def _get_effective_messages(self, request: ModelRequest) -> list[AnyMessage]:
        """Generate effective messages for model call based on summarization event.

        Delegates to `_apply_event_to_messages` so the defensive checks
        (malformed event, out-of-bounds cutoff) are shared with the compact
        tool path.

        Args:
            request: The model request with messages from state.

        Returns:
            The effective message list to use for the model call.
        """
        event = request.state.get("_summarization_event")
        return self._apply_event_to_messages(request.messages, event)

    @staticmethod
    def _apply_event_to_messages(
        messages: list[AnyMessage],
        event: SummarizationEvent | None,
    ) -> list[AnyMessage]:
        """Reconstruct effective messages from raw state messages and a summarization event.

        When a prior summarization event exists, the effective conversation is
        the summary message followed by all messages from `cutoff_index` onward.

        Args:
            messages: Full message list from state.
            event: The `_summarization_event` dict, or `None`.

        Returns:
            The effective message list the model would see.
        """
        if event is None:
            return list(messages)

        try:
            summary_msg = event["summary_message"]
            cutoff_idx = event["cutoff_index"]
        except (KeyError, TypeError) as exc:
            logger.warning("Malformed _summarization_event (missing keys): %s", exc)
            return list(messages)

        if cutoff_idx > len(messages):
            logger.warning(
                "Summarization cutoff_index %d exceeds message count %d; remaining slice will be empty",
                cutoff_idx,
                len(messages),
            )
            return [summary_msg]

        result: list[AnyMessage] = [summary_msg]
        result.extend(messages[cutoff_idx:])
        return result

    @staticmethod
    def _compute_state_cutoff(
        event: SummarizationEvent | None,
        effective_cutoff: int,
    ) -> int:
        """Translate an effective-list cutoff index to an absolute state index.

        When a prior summarization event exists, the effective message list
        starts with the summary message at index 0. The -1 accounts for the
        summary message at effective index 0, which does not correspond to a
        real state message -- the effective cutoff already counts it, so we
        subtract 1 to avoid double-counting.

        Args:
            event: The prior `_summarization_event`, or `None`.
            effective_cutoff: Cutoff index within the effective message list.

        Returns:
            The absolute cutoff index for the state.
        """
        if event is None:
            return effective_cutoff
        prior_cutoff = event.get("cutoff_index")
        if not isinstance(prior_cutoff, int):
            logger.warning("Malformed _summarization_event: missing cutoff_index")
            return effective_cutoff
        return prior_cutoff + effective_cutoff - 1

    def _should_truncate_args(self, messages: list[AnyMessage], total_tokens: int) -> bool:
        """Check if argument truncation should be triggered.

        Args:
            messages: Current message history.
            total_tokens: Total token count of messages.

        Returns:
            True if truncation should occur, False otherwise.
        """
        if self._truncate_args_trigger is None:
            return False

        trigger_type, trigger_value = self._truncate_args_trigger

        if trigger_type == "messages":
            return len(messages) >= trigger_value
        if trigger_type == "tokens":
            return total_tokens >= trigger_value
        if trigger_type == "fraction":
            max_input_tokens = self._get_profile_limits()
            if max_input_tokens is None:
                return False
            threshold = int(max_input_tokens * trigger_value)
            if threshold <= 0:
                threshold = 1
            return total_tokens >= threshold

        return False

    def _determine_truncate_cutoff_index(self, messages: list[AnyMessage]) -> int:
        """Determine the cutoff index for argument truncation based on keep policy.

        Messages at index >= cutoff should be preserved without truncation.
        Messages at index < cutoff can have their tool args truncated.

        Args:
            messages: Current message history.

        Returns:
            Index where truncation cutoff occurs. Messages before this index
            should have args truncated, messages at/after should be preserved.
        """
        keep_type, keep_value = self._truncate_args_keep

        if keep_type == "messages":
            # Keep the most recent N messages
            if len(messages) <= keep_value:
                return len(messages)  # All messages are recent
            return int(len(messages) - keep_value)

        if keep_type in {"tokens", "fraction"}:
            # Calculate target token count
            if keep_type == "fraction":
                max_input_tokens = self._get_profile_limits()
                if max_input_tokens is None:
                    # Fallback to message count if profile not available
                    messages_to_keep = 20
                    if len(messages) <= messages_to_keep:
                        return len(messages)
                    return len(messages) - messages_to_keep
                target_token_count = int(max_input_tokens * keep_value)
            else:
                target_token_count = int(keep_value)

            if target_token_count <= 0:
                target_token_count = 1

            # Keep recent messages up to token limit
            tokens_kept = 0
            for i in range(len(messages) - 1, -1, -1):
                msg_tokens = self._lc_helper._partial_token_counter([messages[i]])
                if tokens_kept + msg_tokens > target_token_count:
                    return i + 1
                tokens_kept += msg_tokens
            return 0  # All messages are within token limit

        return len(messages)

    def _truncate_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Truncate large arguments in a single tool call.

        Args:
            tool_call: The tool call dictionary to truncate.

        Returns:
            A copy of the tool call with large arguments truncated.
        """
        args = tool_call.get("args", {})

        truncated_args = {}
        modified = False

        for key, value in args.items():
            if isinstance(value, str) and len(value) > self._max_arg_length:
                truncated_args[key] = value[:20] + self._truncation_text
                modified = True
            else:
                truncated_args[key] = value

        if modified:
            return {
                **tool_call,
                "args": truncated_args,
            }
        return tool_call

    def _count_tokens(
        self,
        messages: list[AnyMessage],
        system_message: SystemMessage | None,
        tools: list[BaseTool | dict[str, Any]] | None,
    ) -> int:
        """Count tokens for messages plus an optional system message and tools.

        Args:
            messages: Messages to count.
            system_message: Optional system message prepended before counting.
            tools: Optional tools whose schemas contribute to the count.

        Returns:
            Total token count. Counts without `tools` when the configured
                `token_counter` does not accept a `tools` keyword. When the
                counter's signature is introspectable, a `TypeError` raised inside
                the counter's own body is never masked — it propagates so a broken
                counter is not hidden behind a silently wrong count. Counters whose
                signature cannot be introspected are probed instead, and only there
                does a `TypeError` fall back to counting without `tools`.
        """
        counted_messages = [system_message, *messages] if system_message is not None else messages
        if self._counter_accepts_tools is True:
            # `tools=` is absent from the `TokenCounter` protocol but accepted
            # here: the signature check confirmed the counter takes it.
            return self.token_counter(counted_messages, tools=tools)  # ty: ignore[unknown-argument]
        if self._counter_accepts_tools is False:
            return self.token_counter(counted_messages)
        # Signature could not be introspected; probe defensively. This is the only
        # path that swallows a `TypeError`, and only for counters whose signature
        # is opaque (some C-level callables expose no signature).
        try:
            return self.token_counter(counted_messages, tools=tools)  # ty: ignore[unknown-argument]
        except TypeError:
            return self.token_counter(counted_messages)

    def _truncate_args(
        self,
        messages: list[AnyMessage],
        total_tokens: int,
    ) -> tuple[list[AnyMessage], bool]:
        """Truncate large tool call arguments in old messages.

        Args:
            messages: Messages to potentially truncate.
            total_tokens: Precomputed token count for `messages` (plus system
                message and tools). Counting tools is expensive (schema conversion
                per tool), so the caller counts once and shares the result across
                the truncation and summarization checks.

        Returns:
            Tuple of (truncated_messages, modified). If modified is False,
            truncated_messages is the same as input messages.
        """
        if not self._should_truncate_args(messages, total_tokens):
            return messages, False

        cutoff_index = self._determine_truncate_cutoff_index(messages)
        if cutoff_index >= len(messages):
            return messages, False

        # Process messages before the cutoff
        truncated_messages = []
        modified = False

        for i, msg in enumerate(messages):
            if i < cutoff_index and isinstance(msg, AIMessage) and msg.tool_calls:
                # Check if this AIMessage has tool calls we need to truncate
                truncated_tool_calls = []
                msg_modified = False

                for tool_call in msg.tool_calls:
                    if tool_call["name"] in {"write_file", "edit_file"}:
                        truncated_call = self._truncate_tool_call(tool_call)
                        if truncated_call != tool_call:
                            msg_modified = True
                        truncated_tool_calls.append(truncated_call)
                    else:
                        truncated_tool_calls.append(tool_call)

                if msg_modified:
                    # Create a new AIMessage with truncated tool calls
                    truncated_msg = msg.model_copy()
                    truncated_msg.tool_calls = truncated_tool_calls
                    truncated_messages.append(truncated_msg)
                    modified = True
                else:
                    truncated_messages.append(msg)
            else:
                truncated_messages.append(msg)

        return truncated_messages, modified

    def _offload_inline_media(
        self,
        backend: BackendProtocol,
        messages: list[AnyMessage],
    ) -> tuple[list[AnyMessage], int]:
        """Decode inline `data:` media blocks to files and replace them with path references.

        Covers any inline `data:` URL (base64 or percent-encoded/plaintext), not
        just base64, because the XML history renderer drops every inline `data:`
        URL. The caller uploads media before both `_offload_to_backend` and
        `_create_summary`, so both paths receive messages with inline data replaced
        by path references (or error placeholders when an upload fails).

        Each unique media file is uploaded once to
        `{history_path_prefix}/media/{sha256[:16]}.{ext}`. Identical media across
        messages are deduped by content hash. Backends that do not implement
        `upload_files` (raising `NotImplementedError`) degrade to failed-offload
        placeholders rather than crashing the summarization pass.

        Args:
            backend: Backend to write media files to.
            messages: Messages to process.

        Returns:
            A `(messages, failed_block_count)` tuple. Message count and order are
                never changed — only content blocks are rewritten.
        """
        path_map: dict[str, str] = {}  # key -> backend path (successfully uploaded)
        failed_keys: set[str] = set()  # keys whose upload failed
        saw_inline_media = False

        # First pass: upload each unique media file individually for per-block failure tracking.
        for msg in messages:
            for block in msg.content_blocks:
                data_url = _extract_data_url(block)
                if data_url is None:
                    continue
                saw_inline_media = True
                decoded = _decode_data_url(data_url)
                if decoded is None:
                    continue  # undecodable; rewrite emits a failed-offload placeholder
                raw, ext, _mime = decoded
                key = hashlib.sha256(raw).hexdigest()[:16]
                if key in path_map or key in failed_keys:
                    continue
                media_path = f"{self._media_prefix}/{key}.{ext}"
                try:
                    responses = backend.upload_files([(media_path, raw)])
                    if error := _upload_response_error(responses):
                        logger.warning("Failed to upload media %s to backend: %s", media_path, error)
                        failed_keys.add(key)
                        continue
                    path_map[key] = media_path
                except Exception as e:
                    logger.warning("Failed to upload media %s to backend: %s: %s", media_path, type(e).__name__, e)
                    failed_keys.add(key)

        if not saw_inline_media:
            return messages, 0  # no inline media present; return originals unchanged

        return _rewrite_data_url_blocks(messages, path_map)

    async def _aoffload_inline_media(
        self,
        backend: BackendProtocol,
        messages: list[AnyMessage],
    ) -> tuple[list[AnyMessage], int]:
        """Async twin of `_offload_inline_media` using `aupload_files`.

        Args:
            backend: Backend to write media files to.
            messages: Messages to process.

        Returns:
            A `(messages, failed_block_count)` tuple. See `_offload_inline_media`
                for the full contract.
        """
        path_map: dict[str, str] = {}
        failed_keys: set[str] = set()
        saw_inline_media = False

        for msg in messages:
            for block in msg.content_blocks:
                data_url = _extract_data_url(block)
                if data_url is None:
                    continue
                saw_inline_media = True
                decoded = _decode_data_url(data_url)
                if decoded is None:
                    continue  # undecodable; rewrite emits a failed-offload placeholder
                raw, ext, _mime = decoded
                key = hashlib.sha256(raw).hexdigest()[:16]
                if key in path_map or key in failed_keys:
                    continue
                media_path = f"{self._media_prefix}/{key}.{ext}"
                try:
                    responses = await backend.aupload_files([(media_path, raw)])
                    if error := _upload_response_error(responses):
                        logger.warning("Failed to upload media %s to backend: %s", media_path, error)
                        failed_keys.add(key)
                        continue
                    path_map[key] = media_path
                except Exception as e:
                    logger.warning("Failed to upload media %s to backend: %s: %s", media_path, type(e).__name__, e)
                    failed_keys.add(key)

        if not saw_inline_media:
            return messages, 0

        return _rewrite_data_url_blocks(messages, path_map)

    def _warn_media_offload_failures(self, file_path: str | None, failed_media: int) -> None:
        """Warn about an offload failure, or about media that became placeholders.

        Args:
            file_path: Path where history was offloaded, or `None` when the offload
                failed entirely.
            failed_media: Number of media blocks rewritten to failed-offload
                placeholders.
        """
        if file_path is None:
            msg = "Offloading conversation history to backend failed during summarization. Older messages will not be recoverable."
            logger.error(msg)
            warnings.warn(msg, stacklevel=3)
        elif failed_media:
            # History was saved, but some media became failed-offload placeholders.
            # Tie the warning to the saved file so the recovery pointer is honest.
            msg = (
                f"Conversation history was offloaded to {file_path}, but {failed_media} media "
                "block(s) could not be offloaded and appear as failed placeholders in the saved "
                "history; the original media is not recoverable."
            )
            logger.warning(msg)
            warnings.warn(msg, stacklevel=3)

    def _offload_to_backend(
        self,
        backend: BackendProtocol,
        messages: list[AnyMessage],
    ) -> str | None:
        """Persist messages to backend before summarization.

        Appends evicted messages to a single markdown file per thread. Each
        summarization event adds a new section with a timestamp header.

        Previous summary messages are filtered out to avoid redundant storage during
        chained summarization events.

        A `None` return is non-fatal; callers may proceed without the
        offloaded history.

        Args:
            backend: Backend to write to.
            messages: Messages being summarized.

        Returns:
            The file path where history was stored, or `None` if write failed.
        """
        path = self._get_history_path()

        # Filter out previous summary messages to avoid redundant storage
        filtered_messages = self._filter_summary_messages(messages)

        timestamp = datetime.now(UTC).isoformat()
        new_section = f"## Summarized at {timestamp}\n\n{get_buffer_string(filtered_messages, format='xml')}\n\n"

        # Read existing content (if any) and append.
        # Note: We use download_files() instead of read() because read() returns
        # line-numbered content (for LLM consumption), but edit() expects raw content.
        existing_content = ""
        try:
            responses = backend.download_files([path])
            if responses and responses[0].content is not None and responses[0].error is None:
                existing_content = responses[0].content.decode("utf-8")
        except Exception as e:
            # File likely doesn't exist yet, but log for observability
            logger.debug(
                "Exception reading existing history from %s (treating as new file): %s: %s",
                path,
                type(e).__name__,
                e,
            )

        combined_content = existing_content + new_section

        try:
            result = backend.edit(path, existing_content, combined_content) if existing_content else backend.write(path, combined_content)
            if result is None or result.error:
                error_msg = result.error if result else "backend returned None"
                logger.warning(
                    "Failed to offload conversation history to %s (%d messages): %s",
                    path,
                    len(filtered_messages),
                    error_msg,
                )
                return None
        except Exception as e:
            logger.warning(
                "Exception offloading conversation history to %s (%d messages): %s: %s",
                path,
                len(filtered_messages),
                type(e).__name__,
                e,
            )
            return None
        else:
            logger.debug("Offloaded %d messages to %s", len(filtered_messages), path)
            return path

    async def _aoffload_to_backend(
        self,
        backend: BackendProtocol,
        messages: list[AnyMessage],
    ) -> str | None:
        """Persist messages to backend before summarization (async).

        Appends evicted messages to a single markdown file per thread. Each
        summarization event adds a new section with a timestamp header.

        Previous summary messages are filtered out to avoid redundant storage during
        chained summarization events.

        A `None` return is non-fatal; callers may proceed without the
        offloaded history.

        Args:
            backend: Backend to write to.
            messages: Messages being summarized.

        Returns:
            The file path where history was stored, or `None` if write failed.
        """
        path = self._get_history_path()

        # Filter out previous summary messages to avoid redundant storage
        filtered_messages = self._filter_summary_messages(messages)

        timestamp = datetime.now(UTC).isoformat()
        new_section = f"## Summarized at {timestamp}\n\n{get_buffer_string(filtered_messages, format='xml')}\n\n"

        # Read existing content (if any) and append.
        # Note: We use adownload_files() instead of aread() because read() returns
        # line-numbered content (for LLM consumption), but edit() expects raw content.
        existing_content = ""
        try:
            responses = await backend.adownload_files([path])
            if responses and responses[0].content is not None and responses[0].error is None:
                existing_content = responses[0].content.decode("utf-8")
        except Exception as e:
            # File likely doesn't exist yet, but log for observability
            logger.debug(
                "Exception reading existing history from %s (treating as new file): %s: %s",
                path,
                type(e).__name__,
                e,
            )

        combined_content = existing_content + new_section

        try:
            result = (
                await backend.aedit(path, existing_content, combined_content) if existing_content else await backend.awrite(path, combined_content)
            )
            if result is None or result.error:
                error_msg = result.error if result else "backend returned None"
                logger.warning(
                    "Failed to offload conversation history to %s (%d messages): %s",
                    path,
                    len(filtered_messages),
                    error_msg,
                )
                return None
        except Exception as e:
            logger.warning(
                "Exception offloading conversation history to %s (%d messages): %s: %s",
                path,
                len(filtered_messages),
                type(e).__name__,
                e,
            )
            return None
        else:
            logger.debug("Offloaded %d messages to %s", len(filtered_messages), path)
            return path

    def _passthrough_on_summary_failure(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
        preserved_messages: list[AnyMessage],
        truncated_messages: list[AnyMessage],
        new_state_tail: list[AnyMessage],
        *,
        overflow: bool,
    ) -> ModelResponse | ExtendedModelResponse:
        """Heal a turn whose summary generation failed, committing no summary event.

        On overflow the original request was too big, so send the (already
        clipped) preserved tail and persist the tail clip. Otherwise the
        untouched history already fit, so pass it through unchanged. Either way
        the failed-summary sentinel is never recorded as state (v5 CTX-1).

        Args:
            request: The model request.
            handler: The downstream handler.
            preserved_messages: The recent tail kept after the cutoff (clipped on overflow).
            truncated_messages: The full arg-truncated history that fit before compaction.
            new_state_tail: Tail-clip replacements to persist (overflow path only).
            overflow: Whether this turn was triggered by a context overflow.

        Returns:
            The handler response, wrapped to persist the tail clip when present.
        """
        messages = preserved_messages if overflow else truncated_messages
        response = handler(request.override(messages=messages))
        if overflow and new_state_tail:
            return ExtendedModelResponse(
                model_response=response,
                command=Command(update={"messages": list(new_state_tail)}),
            )
        return response

    async def _apassthrough_on_summary_failure(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
        preserved_messages: list[AnyMessage],
        truncated_messages: list[AnyMessage],
        new_state_tail: list[AnyMessage],
        *,
        overflow: bool,
    ) -> ModelResponse | ExtendedModelResponse:
        """Async twin of `_passthrough_on_summary_failure` (v5 CTX-1)."""
        messages = preserved_messages if overflow else truncated_messages
        response = await handler(request.override(messages=messages))
        if overflow and new_state_tail:
            return ExtendedModelResponse(
                model_response=response,
                command=Command(update={"messages": list(new_state_tail)}),
            )
        return response

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        """Process messages before model invocation, with history offloading and arg truncation.

        First applies any previous summarization events to reconstruct the effective message list.
        Then truncates large tool arguments in old messages if configured.
        Finally offloads messages to backend before summarization if thresholds are met.

        Control flow details:

        - If thresholds say "do not summarize", we still attempt one normal
            model call with the current effective/truncated messages.
        - If that call raises `ContextOverflowError` -- or a provider-side
            context-length rejection such as an HTTP 400 "prompt is too long"
            -- we immediately fall back to the summarization path and retry
            the model call with `summary_message + preserved_recent_messages`.

        Unlike the legacy `before_model` approach, this does NOT modify the LangGraph state.
        Instead, it tracks summarization events in middleware state and modifies the model
        request directly.

        Args:
            request: The model request to process.
            handler: The handler to call with the (possibly modified) request.

        Returns:
            A plain `ModelResponse` when no summarization event is created, or
                an `ExtendedModelResponse` that updates `_summarization_event`
                with `cutoff_index`, `summary_message`, and `file_path`.

                If `cutoff_index <= 0`, no compaction occurs and no
                `_summarization_event` update is emitted.
        """
        # Get effective messages based on previous summarization events
        effective_messages = self._get_effective_messages(request)

        # Count once; tool-schema conversion makes each count expensive, so the
        # count is shared between the truncation check and the summarize check.
        total_tokens = self._count_tokens(effective_messages, request.system_message, request.tools)

        # Step 1: Truncate args if configured
        truncated_messages, truncate_modified = self._truncate_args(effective_messages, total_tokens)

        # Step 2: Check if summarization should happen
        if truncate_modified:
            total_tokens = self._count_tokens(truncated_messages, request.system_message, request.tools)
        should_summarize = self._should_summarize(truncated_messages, total_tokens)

        # If no summarization needed, return with truncated messages
        overflow_triggered = False
        if not should_summarize:
            try:
                return handler(request.override(messages=truncated_messages))
            except ContextOverflowError:
                overflow_triggered = True
                # Fallback to summarization on context overflow
            except Exception as exc:
                if not is_context_length_error(exc):
                    raise
                # Provider-side context-length rejection (e.g. HTTP 400
                # "prompt is too long"). Compacting + retrying heals it just
                # like an in-framework overflow; the compacted retry below is
                # unwrapped, so an unrelated error re-surfaces after one attempt.
                overflow_triggered = True

        # Step 3: Perform summarization
        cutoff_index = self._determine_cutoff_index(truncated_messages)
        if cutoff_index <= 0:
            # Can't summarize, return truncated messages
            return handler(request.override(messages=truncated_messages))

        messages_to_summarize, preserved_messages = self._partition_messages(truncated_messages, cutoff_index)

        backend = self._get_backend(request.state, request.runtime)

        # On overflow, offload the large preserved tail tool-message batch so the
        # retry sends a strictly smaller payload instead of re-sending the same
        # oversized tail (which would wedge the agent permanently).
        new_state_tail: list[AnyMessage] = []
        if overflow_triggered:
            preserved_messages, new_state_tail = _clip_overflow_tail(
                preserved_messages,
                backend,
                keep=self._lc_helper.keep,
                max_input_tokens=self._get_profile_limits(),
                token_counter=self.token_counter,
                large_tool_results_prefix=self._large_tool_results_prefix,
            )

        # Upload inline media once so both offload and summary see path references.
        offloaded_media_messages, failed_media = self._offload_inline_media(backend, messages_to_summarize)

        # Offload to backend first so history is preserved before summarization.
        # If offload fails, summarization still proceeds (with file_path=None).
        file_path = self._offload_to_backend(backend, offloaded_media_messages)
        self._warn_media_offload_failures(file_path, failed_media)

        # Generate summary
        summary = self._create_summary(offloaded_media_messages)

        # A failed summary must never become permanent state (v5 CTX-1): the
        # error string would replace all pre-cutoff history on every future
        # turn. Skip the event commit and heal without it — on overflow, send
        # the (already clipped) preserved tail so the retry is strictly smaller;
        # otherwise the untouched history already fit, so pass it through.
        if _summary_failed(summary):
            logger.warning("summarization: summary generation failed; skipping compaction this turn (%s)", summary)
            return self._passthrough_on_summary_failure(
                request, handler, preserved_messages, truncated_messages, new_state_tail, overflow=overflow_triggered
            )

        # Build summary message with file path reference
        new_messages = self._build_new_messages_with_path(summary, file_path)

        previous_event = request.state.get("_summarization_event")
        state_cutoff_index = self._compute_state_cutoff(previous_event, cutoff_index)

        # Create new summarization event
        new_event: SummarizationEvent = {
            "cutoff_index": state_cutoff_index,
            "summary_message": new_messages[0],  # The HumanMessage with summary  # ty: ignore[invalid-argument-type]
            "file_path": file_path,
        }

        # Modify request to use summarized messages
        modified_messages = [*new_messages, *preserved_messages]
        response = handler(request.override(messages=modified_messages))

        update: dict[str, Any] = {"_summarization_event": new_event}
        if new_state_tail:
            # Replacements carry the original message ids, so `add_messages`
            # overwrites in place -- count and order are unchanged.
            update["messages"] = list(new_state_tail)

        # Return ExtendedModelResponse with state update
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update=update),
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        """Process messages before model invocation, with history offloading and arg truncation (async).

        First applies any previous summarization events to reconstruct the effective message list.
        Then truncates large tool arguments in old messages if configured.
        Finally offloads messages to backend before summarization if thresholds are met.

        Control flow details:

        - If thresholds say "do not summarize", we still attempt one normal
            model call with the current effective/truncated messages.
        - If that call raises `ContextOverflowError` (or a provider-side
            context-length rejection such as an HTTP 400 "prompt is too long"),
            we immediately fall back to the summarization path and retry the
            model call with `summary_message + preserved_recent_messages`.

        Unlike the legacy `abefore_model` approach, this does NOT modify the LangGraph state.
        Instead, it tracks summarization events in middleware state and modifies the model
        request directly.

        Args:
            request: The model request to process.
            handler: The handler to call with the (possibly modified) request.

        Returns:
            A plain `ModelResponse` when no summarization event is created, or
                an `ExtendedModelResponse` that updates `_summarization_event`
                with `cutoff_index`, `summary_message`, and `file_path`.

                If `cutoff_index <= 0`, no compaction occurs and no
                `_summarization_event` update is emitted.
        """
        # Get effective messages based on previous summarization events
        effective_messages = self._get_effective_messages(request)

        # Count once; tool-schema conversion makes each count expensive, so the
        # count is shared between the truncation check and the summarize check.
        total_tokens = self._count_tokens(effective_messages, request.system_message, request.tools)

        # Step 1: Truncate args if configured
        truncated_messages, truncate_modified = self._truncate_args(effective_messages, total_tokens)

        # Step 2: Check if summarization should happen
        if truncate_modified:
            total_tokens = self._count_tokens(truncated_messages, request.system_message, request.tools)
        should_summarize = self._should_summarize(truncated_messages, total_tokens)

        # If no summarization needed, return with truncated messages
        overflow_triggered = False
        if not should_summarize:
            try:
                return await handler(request.override(messages=truncated_messages))
            except ContextOverflowError:
                overflow_triggered = True
                # Fallback to summarization on context overflow
            except Exception as exc:
                if not is_context_length_error(exc):
                    raise
                # Provider-side context-length rejection; compact and retry
                # (see the sync path for the rationale).
                overflow_triggered = True

        # Step 3: Perform summarization
        cutoff_index = self._determine_cutoff_index(truncated_messages)
        if cutoff_index <= 0:
            # Can't summarize, return truncated messages
            return await handler(request.override(messages=truncated_messages))

        messages_to_summarize, preserved_messages = self._partition_messages(truncated_messages, cutoff_index)

        backend = self._get_backend(request.state, request.runtime)

        # On overflow, offload the large preserved tail tool-message batch so the
        # retry sends a strictly smaller payload instead of re-sending the same
        # oversized tail (which would wedge the agent permanently).
        new_state_tail: list[AnyMessage] = []
        if overflow_triggered:
            preserved_messages, new_state_tail = await _aclip_overflow_tail(
                preserved_messages,
                backend,
                keep=self._lc_helper.keep,
                max_input_tokens=self._get_profile_limits(),
                token_counter=self.token_counter,
                large_tool_results_prefix=self._large_tool_results_prefix,
            )

        # Upload inline media once so both offload and summary see path references.
        # This must complete before the gather since both coroutines consume the result.
        offloaded_media_messages, failed_media = await self._aoffload_inline_media(backend, messages_to_summarize)

        # Offload to backend and generate summary concurrently -- they are independent.
        # If offload fails, summarization still proceeds (with file_path=None).
        file_path, summary = await asyncio.gather(
            self._aoffload_to_backend(backend, offloaded_media_messages),
            self._acreate_summary(offloaded_media_messages),
        )
        self._warn_media_offload_failures(file_path, failed_media)

        # See the sync path: never commit a failed summary as permanent state
        # (v5 CTX-1).
        if _summary_failed(summary):
            logger.warning("summarization: summary generation failed; skipping compaction this turn (%s)", summary)
            return await self._apassthrough_on_summary_failure(
                request, handler, preserved_messages, truncated_messages, new_state_tail, overflow=overflow_triggered
            )

        # Build summary message with file path reference
        new_messages = self._build_new_messages_with_path(summary, file_path)

        previous_event = request.state.get("_summarization_event")
        state_cutoff_index = self._compute_state_cutoff(previous_event, cutoff_index)

        # Create new summarization event
        new_event: SummarizationEvent = {
            "cutoff_index": state_cutoff_index,
            "summary_message": new_messages[0],  # The HumanMessage with summary  # ty: ignore[invalid-argument-type]
            "file_path": file_path,
        }

        # Modify request to use summarized messages
        modified_messages = [*new_messages, *preserved_messages]
        response = await handler(request.override(messages=modified_messages))

        update: dict[str, Any] = {"_summarization_event": new_event}
        if new_state_tail:
            # Replacements carry the original message ids, so `add_messages`
            # overwrites in place -- count and order are unchanged.
            update["messages"] = list(new_state_tail)

        # Return ExtendedModelResponse with state update
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update=update),
        )


# Public alias
SummarizationMiddleware = _BogAgentsSummarizationMiddleware


def create_summarization_middleware(
    model: BaseChatModel,
    backend: BACKEND_TYPES,
    *,
    summary_prompt: str = BOG_DEFAULT_SUMMARY_PROMPT,
    trim_tokens_to_summarize: int | None = None,
    token_counter: TokenCounter = count_tokens_approximately,
) -> _BogAgentsSummarizationMiddleware:
    """Create a `SummarizationMiddleware` with model-aware defaults.

    Computes trigger, keep, and truncation settings from the model's profile
    (or uses fixed-token fallbacks) and returns a configured middleware.

    Args:
        model: Resolved chat model instance.
        backend: Backend instance or factory for persisting conversation history.
        summary_prompt: Prompt template for generating summaries.
        trim_tokens_to_summarize: Max tokens to include when generating a summary.
            `None` (the default) disables trimming.
        token_counter: Function to count tokens in messages.

    Returns:
        Configured `SummarizationMiddleware` instance.

    Raises:
        TypeError: If `model` is not a `BaseChatModel` instance.
    """
    from langchain.chat_models import BaseChatModel as RuntimeBaseChatModel

    if not isinstance(model, RuntimeBaseChatModel):
        msg = "`create_summarization_middleware` expects `model` to be a `BaseChatModel` instance."
        raise TypeError(msg)

    defaults = compute_summarization_defaults(model)
    return SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=defaults["trigger"],
        keep=defaults["keep"],
        token_counter=token_counter,
        summary_prompt=summary_prompt,
        trim_tokens_to_summarize=trim_tokens_to_summarize,
        truncate_args_settings=defaults["truncate_args_settings"],
    )


def create_summarization_tool_middleware(
    model: str | BaseChatModel,
    backend: BACKEND_TYPES,
) -> SummarizationToolMiddleware:
    """Create a `SummarizationToolMiddleware` with model-aware defaults.

    Convenience factory that creates a `SummarizationMiddleware` via
    `create_summarization_middleware` and wraps it in a
    `SummarizationToolMiddleware`.

    Args:
        model: Chat model instance or model string (e.g., `"anthropic:claude-sonnet-4-20250514"`).
        backend: Backend instance or factory for persisting conversation history.

    Returns:
        Configured `SummarizationToolMiddleware` instance.

    Example:
        Using the default `StateBackend`:

        ```python
        from bog_agents import create_agent
        from bog_agents.backends import StateBackend
        from bog_agents.middleware.summarization import (
            create_summarization_tool_middleware,
        )

        model = "openai:gpt-5.4"
        agent = create_agent(
            model=model,
            middleware=[
                create_summarization_tool_middleware(model, StateBackend),
            ],
        )
        ```

        Using a custom backend instance (e.g., Daytona Sandbox):

        ```python
        from daytona import Daytona
        from bog_agents import create_agent
        from bog_agents.middleware.summarization import (
            create_summarization_tool_middleware,
        )
        from langchain_daytona import DaytonaSandbox

        sandbox = Daytona().create()
        backend = DaytonaSandbox(sandbox=sandbox)
        model = "openai:gpt-5.4"
        agent = create_agent(
            model=model,
            backend=backend,
            middleware=[
                create_summarization_tool_middleware(model, backend),
            ],
        )
        ```
    """
    from bog_agents._models import resolve_model

    if isinstance(model, str):
        model = resolve_model(model)
    summarization = create_summarization_middleware(model, backend)
    return SummarizationToolMiddleware(summarization)


class SummarizationToolMiddleware(AgentMiddleware):
    """Middleware that provides a `compact_conversation` tool for manual compaction.

    This middleware composes with a `SummarizationMiddleware` instance, reusing
    its summarization engine (model, backend, trigger thresholds) to let the
    agent compact its own context window.

    This middleware never compacts automatically. Compaction only occurs when
    `compact_conversation` is called as a normal tool call (by the model or by
    an explicit user action, e.g. as implemented in the bog-agents-cli).

    To avoid compacting too early, compact tool execution is gated by
    `_is_eligible_for_compaction`, which requires reported usage to reach about
    50% of the configured auto-summarization trigger.

    The tool and auto-summarization share the same `_summarization_event` state
    key, so they interoperate correctly.

    For a simpler setup, use `create_summarization_tool_middleware` which
    handles both steps.

    Example:
        ```python
        from bog_agents.middleware.summarization import (
            SummarizationMiddleware,
            SummarizationToolMiddleware,
        )

        summ = SummarizationMiddleware(model="gpt-4o-mini", backend=backend)
        tool_mw = SummarizationToolMiddleware(summ)

        agent = create_agent(middleware=[summ, tool_mw])
        ```
    """

    state_schema = SummarizationState

    def __init__(self, summarization: _BogAgentsSummarizationMiddleware) -> None:
        """Initialize with a reference to the summarization middleware.

        Args:
            summarization: The `SummarizationMiddleware` instance whose
                summarization engine this tool will delegate to.
        """
        self._summarization = summarization
        self.tools: list[BaseTool] = [self._create_compact_tool()]

    def _resolve_backend(self, runtime: ToolRuntime) -> BackendProtocol:
        """Resolve backend from instance or factory using a `ToolRuntime`.

        Args:
            runtime: The tool runtime context.

        Returns:
            Resolved backend instance.
        """
        backend = self._summarization._backend
        if callable(backend):
            return backend(runtime)  # ty: ignore[call-top-callable]
        return backend

    def _create_compact_tool(self) -> BaseTool:
        """Create the `compact_conversation` structured tool.

        Returns:
            A `StructuredTool` with both sync and async implementations.
        """
        from langchain_core.tools import StructuredTool

        mw = self

        def sync_compact(runtime: ToolRuntime) -> Command:
            return mw._run_compact(runtime)

        async def async_compact(runtime: ToolRuntime) -> Command:
            return await mw._arun_compact(runtime)

        return StructuredTool.from_function(
            name="compact_conversation",
            description=(
                "Compact the conversation by summarizing older messages "
                "into a concise summary. Use this proactively when the "
                "conversation is getting long to free up context window "
                "space. This tool takes no arguments."
            ),
            func=sync_compact,
            coroutine=async_compact,
        )

    def _build_compact_result(
        self,
        runtime: ToolRuntime,
        to_summarize: list[AnyMessage],
        summary: str,
        file_path: str | None,
        event: SummarizationEvent | None,
        cutoff: int,
    ) -> Command:
        """Build the `Command` result for a successful compact operation.

        Shared by both sync and async compact paths to avoid duplicating
        the event construction and cutoff arithmetic.

        Args:
            runtime: The tool runtime context.
            to_summarize: Messages that were summarized.
            summary: The generated summary text.
            file_path: Backend path where history was offloaded, or `None`.
            event: The prior `_summarization_event`, or `None`.
            cutoff: The cutoff index within the effective message list.

        Returns:
            A `Command` with `_summarization_event` state update and a
            confirmation `ToolMessage`.
        """
        s = self._summarization
        summary_msg = s._build_new_messages_with_path(summary, file_path)[0]
        state_cutoff = s._compute_state_cutoff(event, cutoff)

        new_event: SummarizationEvent = {
            "cutoff_index": state_cutoff,
            "summary_message": summary_msg,
            "file_path": file_path,
        }

        return Command(
            update={
                "_summarization_event": new_event,
                "messages": [
                    ToolMessage(
                        content=f"Conversation compacted. Summarized {len(to_summarize)} messages into a concise summary.",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    @staticmethod
    def _nothing_to_compact(tool_call_id: str) -> Command:
        """Return a "nothing to compact" result for the compact tool.

        Args:
            tool_call_id: The originating tool call ID.

        Returns:
            A `Command` with a descriptive `ToolMessage`.
        """
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Nothing to compact yet \u2014 conversation is within the token budget.",
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )

    @staticmethod
    def _compact_error(tool_call_id: str, exc: BaseException) -> Command:
        """Return an error result for the compact tool.

        Args:
            tool_call_id: The originating tool call ID.
            exc: The exception that caused the failure.

        Returns:
            A `Command` with an error `ToolMessage`.
        """
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            "Compaction failed: an error occurred while "
                            f"generating the summary ({type(exc).__name__}: "
                            f"{exc}). The conversation has not been compacted "
                            "— no messages were summarized or removed."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )

    @staticmethod
    def _compact_threshold(value: float) -> int:
        """Return the half-trigger threshold used by the compact tool.

        Args:
            value: The configured trigger value.

        Returns:
            Half the trigger value, floored at 1.
        """
        return max(1, int(value * 0.5))

    @staticmethod
    def _compact_trigger_clause(condition: object) -> Mapping[str, float]:
        """Normalize tuple and dict trigger conditions for compact gating.

        Args:
            condition: A `ContextSize` tuple or a `TriggerClause` mapping.

        Returns:
            The condition as a mapping of threshold kind to value.
        """
        if isinstance(condition, Mapping):
            return cast("Mapping[str, float]", condition)
        kind, value = cast("tuple[str, float]", condition)
        return {kind: value}

    def _is_compaction_clause_met(self, clause: Mapping[str, float], messages: list[AnyMessage]) -> bool:
        """Check whether a normalized compact eligibility clause is met.

        A clause has AND semantics: every threshold it declares must be reached.

        Args:
            clause: A normalized trigger clause.
            messages: The effective conversation messages.

        Returns:
            Whether every threshold in the clause is met.
        """
        lc = self._summarization._lc_helper
        for kind, value in clause.items():
            if kind == "messages":
                if len(messages) < self._compact_threshold(value):
                    return False
            elif kind == "tokens":
                if not lc._should_summarize_based_on_reported_tokens(messages, self._compact_threshold(value)):
                    return False
            elif kind == "fraction":
                max_input_tokens = lc._get_profile_limits()
                if max_input_tokens is None:
                    return False
                threshold = self._compact_threshold(max_input_tokens * value)
                if not lc._should_summarize_based_on_reported_tokens(messages, threshold):
                    return False
            else:
                return False
        return True

    def _is_eligible_for_compaction(self, messages: list[AnyMessage]) -> bool:
        """Check if manual compaction is currently allowed.

        This is an eligibility gate for `compact_conversation` tool calls, not a
        background trigger. The conversation must be at or above about 50% of the
        configured auto-summarization trigger:

        - For `("tokens", N)`, eligibility starts at `0.5 * N`.
        - For `("messages", N)`, eligibility starts at `0.5 * N` messages.
        - For `("fraction", F)`, eligibility starts at `0.5 * F` of model max input
            tokens.
        - For dict clauses, all specified thresholds must be met.

        Clauses are combined with OR semantics. Uses reported usage metadata when
        available.

        Args:
            messages: The effective conversation messages.

        Returns:
            Whether manual compaction is allowed right now.
        """
        trigger_clauses = self._summarization._lc_helper._trigger_clauses
        if not trigger_clauses:
            return False
        return any(self._is_compaction_clause_met(self._compact_trigger_clause(condition), messages) for condition in trigger_clauses)

    def _run_compact(self, runtime: ToolRuntime) -> Command:
        """Synchronous compact implementation called by the compact tool.

        Args:
            runtime: The `ToolRuntime` injected by the tool node.

        Returns:
            A `Command` with `_summarization_event` state update, or a
                `Command` with a "nothing to compact" or error `ToolMessage`.
        """
        s = self._summarization
        tool_call_id = runtime.tool_call_id or ""
        messages = runtime.state.get("messages", [])
        event = runtime.state.get("_summarization_event")
        effective = s._apply_event_to_messages(messages, event)

        if not self._is_eligible_for_compaction(effective):
            return self._nothing_to_compact(tool_call_id)

        cutoff = s._determine_cutoff_index(effective)
        if cutoff == 0:
            return self._nothing_to_compact(tool_call_id)

        try:
            to_summarize, _ = s._partition_messages(effective, cutoff)
            summary = s._create_summary(to_summarize)
            backend = self._resolve_backend(runtime)
            file_path = s._offload_to_backend(backend, to_summarize)
        except Exception as exc:  # tool must return a ToolMessage, not raise
            logger.exception("compact_conversation tool failed")
            return self._compact_error(tool_call_id, exc)

        # A swallowed summary error must not be committed as the summary (v5 CTX-1).
        if _summary_failed(summary):
            return self._compact_error(tool_call_id, RuntimeError(summary))

        return self._build_compact_result(runtime, to_summarize, summary, file_path, event, cutoff)

    async def _arun_compact(self, runtime: ToolRuntime) -> Command:
        """Async variant of `_run_compact`. See that method for details.

        Args:
            runtime: The `ToolRuntime` injected by the tool node.

        Returns:
            A `Command` with `_summarization_event` state update, or a
                `Command` with a "nothing to compact" or error `ToolMessage`.
        """
        s = self._summarization
        tool_call_id = runtime.tool_call_id or ""
        messages = runtime.state.get("messages", [])
        event = runtime.state.get("_summarization_event")
        effective = s._apply_event_to_messages(messages, event)

        if not self._is_eligible_for_compaction(effective):
            return self._nothing_to_compact(tool_call_id)

        cutoff = s._determine_cutoff_index(effective)
        if cutoff == 0:
            return self._nothing_to_compact(tool_call_id)

        try:
            to_summarize, _ = s._partition_messages(effective, cutoff)
            summary = await s._acreate_summary(to_summarize)
            backend = self._resolve_backend(runtime)
            file_path = await s._aoffload_to_backend(backend, to_summarize)
        except Exception as exc:  # tool must return a ToolMessage, not raise
            logger.exception("compact_conversation tool failed")
            return self._compact_error(tool_call_id, exc)

        # A swallowed summary error must not be committed as the summary (v5 CTX-1).
        if _summary_failed(summary):
            return self._compact_error(tool_call_id, RuntimeError(summary))

        return self._build_compact_result(runtime, to_summarize, summary, file_path, event, cutoff)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject a compact-tool usage nudge into the system prompt.

        This only updates prompt text so the model can decide whether to call
        `compact_conversation` earlier in long sessions. It does not execute the
        tool automatically.

        Args:
            request: The model request to process.
            handler: The handler to call with the modified request.

        Returns:
            The model response from the handler.
        """
        new_system_message = append_to_system_message(request.system_message, SUMMARIZATION_SYSTEM_PROMPT)
        return handler(request.override(system_message=new_system_message))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Inject a compact-tool usage nudge into the system prompt (async).

        This only updates prompt text so the model can decide whether to call
        `compact_conversation` earlier in long sessions. It does not execute the
        tool automatically.

        Args:
            request: The model request to process.
            handler: The handler to call with the modified request.

        Returns:
            The model response from the handler.
        """
        new_system_message = append_to_system_message(request.system_message, SUMMARIZATION_SYSTEM_PROMPT)
        return await handler(request.override(system_message=new_system_message))
