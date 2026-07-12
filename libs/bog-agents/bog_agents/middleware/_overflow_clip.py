"""Read-side clipping for the summarization-on-overflow fallback path.

When `SummarizationMiddleware`'s `wrap_model_call` catches a
`ContextOverflowError`, it falls through to summarization and *also* invokes
`_clip_overflow_tail` (or its async variant) to shrink the trailing
`ToolMessage` batch in the preserved suffix. Without this, the retry after an
overflow re-sends the same oversized tail and the agent wedges permanently.

Two per-`ToolMessage` paths:

- `read_file` tool result: head-slice the content and append a notice pointing
    back to the original `file_path` argument. No new backend write is needed
    because the original file already lives at that path.
- Any other tool result: full offload to `{large_tool_results_prefix}/{tool_call_id}`
    via the shared eviction helper, then replace the message with a
    `TOO_LARGE_TOOL_MSG` stub.

Replacements preserve the original message `id` so the `add_messages` reducer
overwrites the originals in place — message count and order never change.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from bog_agents.middleware._message_eviction import (
    _aoffload_tool_message_content,
    _extract_text_from_message,
    _offload_tool_message_content,
)

if TYPE_CHECKING:
    from langchain.agents.middleware.summarization import ContextSize, TokenCounter

    from bog_agents.backends.protocol import BackendProtocol

DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS = 5_000
"""Fallback clip threshold when `keep` carries no token information.

Equivalent to a 20,000-character floor under a `chars / 4` approximation.
"""

READ_FILE_CLIP_HEAD_CHARS = 4_000
"""Characters retained from the head of a clipped `read_file` tool result."""


def _derive_overflow_clip_threshold_tokens(keep: ContextSize, max_input_tokens: int | None) -> int:
    """Derive a token threshold for tail-`ToolMessage` clipping from `keep`.

    Args:
        keep: The summarization retention policy.
        max_input_tokens: Model context window, when known.

    Returns:
        The keep token budget. If `keep` is message-based (no token info), or
            fraction-based with no known context window, returns
            `DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS`.
    """
    kind, value = keep
    if kind == "tokens":
        return int(value)
    if kind == "fraction":
        if max_input_tokens is None:
            return DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS
        return int(max_input_tokens * value)
    return DEFAULT_OVERFLOW_CLIP_THRESHOLD_TOKENS


def _find_tail_tool_message_batch(messages: list[AnyMessage]) -> tuple[int, list[ToolMessage]] | None:
    """Return `(start_index, batch)` if `messages` ends with consecutive `ToolMessage` objects.

    Args:
        messages: The message list to inspect.

    Returns:
        A `(start_index, batch)` tuple, or `None` when the list is empty or does
            not end with a `ToolMessage`.
    """
    if not messages or not isinstance(messages[-1], ToolMessage):
        return None
    i = len(messages) - 1
    while i >= 0 and isinstance(messages[i], ToolMessage):
        i -= 1
    start = i + 1
    return start, [cast("ToolMessage", m) for m in messages[start:]]


def _build_tool_call_index(messages: list[AnyMessage]) -> dict[str, dict[str, Any]]:
    """Map `tool_call_id` to its originating tool-call dict.

    Args:
        messages: Messages to scan for `AIMessage.tool_calls`.

    Returns:
        A mapping of `tool_call_id` to the tool-call dict that produced it.
    """
    index: dict[str, dict[str, Any]] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                tcid = tc.get("id")
                if tcid:
                    index[tcid] = cast("dict[str, Any]", tc)
    return index


def _slice_read_file_tm(msg: ToolMessage, original_path: str) -> ToolMessage:
    """Head-slice a `read_file` `ToolMessage` and append a path-pointer notice.

    `read_file` results don't need a fresh offload write — the full file already
    lives on the backend at `original_path`, and the agent can recover it with
    `read_file(file_path=original_path, offset=N, limit=K)`.

    Args:
        msg: The `read_file` tool result to clip.
        original_path: The `file_path` argument of the originating tool call.

    Returns:
        A copy of `msg` (same `id`) with head-sliced content plus a recovery notice.
    """
    content = _extract_text_from_message(msg)
    notice = (
        "\n\n[Output was truncated due to context window size limits. "
        f"The full content is at {original_path}. "
        "Use read_file with offset and limit parameters to retrieve specific portions. "
        f"For example, to read the first 100 lines, call read_file with file_path='{original_path}', offset=0, limit=100.]"
    )
    return msg.model_copy(update={"content": content[:READ_FILE_CLIP_HEAD_CHARS] + notice})


def _read_file_original_path(msg: ToolMessage, tc_index: dict[str, dict[str, Any]]) -> str | None:
    """Return the `file_path` arg of the matching `read_file` tool call, or `None`.

    Args:
        msg: The tool result message.
        tc_index: Index produced by `_build_tool_call_index`.

    Returns:
        The original `file_path` argument, or `None` when the message did not come
            from a `read_file` call (or the call carried no usable path).
    """
    tc = tc_index.get(msg.tool_call_id) if msg.tool_call_id else None
    if not tc or tc.get("name") != "read_file":
        return None
    path = tc.get("args", {}).get("file_path")
    return path if isinstance(path, str) and path else None


def _clip_one_tail_message(
    msg: ToolMessage,
    tc_index: dict[str, dict[str, Any]],
    backend: BackendProtocol,
    large_tool_results_prefix: str,
) -> ToolMessage | None:
    """Apply the appropriate per-message clip: `read_file` slice vs. generic eviction.

    Args:
        msg: The tail tool result to clip.
        tc_index: Index produced by `_build_tool_call_index`.
        backend: Backend used for the generic-eviction path.
        large_tool_results_prefix: Path prefix for offloaded tool results.

    Returns:
        The clipped replacement, or `None` when the offload write failed.
    """
    original_path = _read_file_original_path(msg, tc_index)
    if original_path is not None:
        return _slice_read_file_tm(msg, original_path)
    return _offload_tool_message_content(msg, _extract_text_from_message(msg), backend, large_tool_results_prefix)


async def _aclip_one_tail_message(
    msg: ToolMessage,
    tc_index: dict[str, dict[str, Any]],
    backend: BackendProtocol,
    large_tool_results_prefix: str,
) -> ToolMessage | None:
    """Async variant of `_clip_one_tail_message`.

    Args:
        msg: The tail tool result to clip.
        tc_index: Index produced by `_build_tool_call_index`.
        backend: Backend used for the generic-eviction path.
        large_tool_results_prefix: Path prefix for offloaded tool results.

    Returns:
        The clipped replacement, or `None` when the offload write failed.
    """
    original_path = _read_file_original_path(msg, tc_index)
    if original_path is not None:
        return _slice_read_file_tm(msg, original_path)
    return await _aoffload_tool_message_content(msg, _extract_text_from_message(msg), backend, large_tool_results_prefix)


def _finalize_clip(
    preserved_messages: list[AnyMessage],
    start: int,
    tail: list[ToolMessage],
    results: list[ToolMessage | None],
) -> tuple[list[AnyMessage], list[AnyMessage]]:
    """Assemble the clipped message list and the replacement batch to persist.

    Args:
        preserved_messages: The original preserved suffix.
        start: Index where the trailing tool-message batch begins.
        tail: The original trailing tool-message batch.
        results: Per-message clip results (`None` where the clip failed).

    Returns:
        A `(clipped_messages, replacements)` tuple. `replacements` is empty when
            nothing was clipped.
    """
    new_tail: list[AnyMessage] = []
    any_clipped = False
    for result, original in zip(results, tail, strict=True):
        if result is None:
            new_tail.append(original)
            continue
        clipped = result if result.id is not None else result.model_copy(update={"id": str(uuid.uuid4())})
        new_tail.append(clipped)
        any_clipped = True
    if not any_clipped:
        return preserved_messages, []
    return [*preserved_messages[:start], *new_tail], new_tail


def _clip_overflow_tail(
    preserved_messages: list[AnyMessage],
    backend: BackendProtocol,
    *,
    keep: ContextSize,
    max_input_tokens: int | None,
    token_counter: TokenCounter,
    large_tool_results_prefix: str,
) -> tuple[list[AnyMessage], list[AnyMessage]]:
    """Offload the trailing `ToolMessage` batch when it is large enough to matter.

    Engages only when `preserved_messages` ends with consecutive `ToolMessage`
    objects whose combined token count reaches
    `_derive_overflow_clip_threshold_tokens()`. Each large tool result is written
    under `{large_tool_results_prefix}/{tool_call_id}` and replaced in-place by an
    offload-pointer `ToolMessage`.

    Args:
        preserved_messages: The suffix that survives summarization.
        backend: Backend to write offloaded tool results to.
        keep: The summarization retention policy, used to derive the threshold.
        max_input_tokens: Model context window, when known.
        token_counter: Callable used to size the trailing batch.
        large_tool_results_prefix: Path prefix for offloaded tool results.

    Returns:
        A `(modified_preserved_messages, replacements)` tuple. Replacements carry
            the original message ids so the `add_messages` reducer overwrites the
            originals when the caller propagates them via a `Command` update. Any
            message whose backend write failed keeps its original in both lists.
    """
    found = _find_tail_tool_message_batch(preserved_messages)
    if found is None:
        return preserved_messages, []
    start, tail = found
    if token_counter(cast("list[AnyMessage]", tail)) < _derive_overflow_clip_threshold_tokens(keep, max_input_tokens):
        return preserved_messages, []
    tc_index = _build_tool_call_index(preserved_messages)
    results = [_clip_one_tail_message(m, tc_index, backend, large_tool_results_prefix) for m in tail]
    return _finalize_clip(preserved_messages, start, tail, results)


async def _aclip_overflow_tail(
    preserved_messages: list[AnyMessage],
    backend: BackendProtocol,
    *,
    keep: ContextSize,
    max_input_tokens: int | None,
    token_counter: TokenCounter,
    large_tool_results_prefix: str,
) -> tuple[list[AnyMessage], list[AnyMessage]]:
    """Async variant of `_clip_overflow_tail`, offloading each tail message concurrently.

    Args:
        preserved_messages: The suffix that survives summarization.
        backend: Backend to write offloaded tool results to.
        keep: The summarization retention policy, used to derive the threshold.
        max_input_tokens: Model context window, when known.
        token_counter: Callable used to size the trailing batch.
        large_tool_results_prefix: Path prefix for offloaded tool results.

    Returns:
        A `(modified_preserved_messages, replacements)` tuple. See
            `_clip_overflow_tail` for the full contract.
    """
    found = _find_tail_tool_message_batch(preserved_messages)
    if found is None:
        return preserved_messages, []
    start, tail = found
    if token_counter(cast("list[AnyMessage]", tail)) < _derive_overflow_clip_threshold_tokens(keep, max_input_tokens):
        return preserved_messages, []
    tc_index = _build_tool_call_index(preserved_messages)
    results = list(await asyncio.gather(*(_aclip_one_tail_message(m, tc_index, backend, large_tool_results_prefix) for m in tail)))
    return _finalize_clip(preserved_messages, start, tail, results)
