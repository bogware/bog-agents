"""Shared helpers for evicting/clipping large message content with a head+tail preview.

Used by:

- `FilesystemMiddleware` — proactive per-tool-call offload when a tool result
    exceeds its configured size threshold.
- `SummarizationMiddleware` — reactive tail-clipping in the fallback
    summarization path after a `ContextOverflowError` (see `_overflow_clip`).

This module is the single implementation. `filesystem.py` imports
`TOO_LARGE_TOOL_MSG`, `_create_content_preview`, `_extract_text_from_message`,
and `_build_evicted_content` from here rather than redefining them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from langchain_core.messages import BaseMessage, ToolMessage

from bog_agents.backends.utils import format_content_with_line_numbers, sanitize_tool_call_id

if TYPE_CHECKING:
    from langchain_core.messages.content import ContentBlock

    from bog_agents.backends.protocol import BackendProtocol

TOO_LARGE_TOOL_MSG = """Tool result too large, the result of this tool call {tool_call_id} was saved in the filesystem at this path: {file_path}

You can read the result from the filesystem by using the read_file tool, but make sure to only read part of the result at a time.

You can do this by specifying an offset and limit in the read_file tool call. For example, to read the first 100 lines, you can use the read_file tool with offset=0 and limit=100.

Here is a preview showing the head and tail of the result (lines of the form `... [N lines truncated] ...` indicate omitted lines in the middle of the content):

{content_sample}
"""


def _create_content_preview(content_str: str, *, head_lines: int = 5, tail_lines: int = 5) -> str:
    """Create a preview of content showing head and tail with a truncation marker.

    Args:
        content_str: The full content string to preview.
        head_lines: Number of lines to show from the start.
        tail_lines: Number of lines to show from the end.

    Returns:
        Formatted preview string with line numbers.
    """
    lines = content_str.splitlines()

    if len(lines) <= head_lines + tail_lines:
        # If content is small enough, show all lines.
        preview_lines = [line[:1000] for line in lines]
        return format_content_with_line_numbers(preview_lines, start_line=1)

    head = [line[:1000] for line in lines[:head_lines]]
    tail = [line[:1000] for line in lines[-tail_lines:]]

    head_sample = format_content_with_line_numbers(head, start_line=1)
    truncation_notice = f"\n... [{len(lines) - head_lines - tail_lines} lines truncated] ...\n"
    tail_sample = format_content_with_line_numbers(tail, start_line=len(lines) - tail_lines + 1)

    return head_sample + truncation_notice + tail_sample


def _extract_text_from_message(message: BaseMessage) -> str:
    """Extract text from a message using its `content_blocks` property.

    Joins all text content blocks and ignores non-text blocks (images, audio, etc.)
    so that binary payloads don't inflate the size measurement.

    Args:
        message: The message to extract text from.

    Returns:
        Joined text from all text content blocks.
    """
    texts = [block["text"] for block in message.content_blocks if block["type"] == "text"]
    return "\n".join(texts)


def _build_evicted_content(message: ToolMessage, replacement_text: str) -> str | list[ContentBlock]:
    """Build replacement content for an evicted message, preserving non-text blocks.

    For plain string content, returns the replacement text directly. For list content
    with mixed block types (e.g. text + image), replaces all text blocks with a single
    text block containing the replacement text while keeping non-text blocks intact.

    Args:
        message: The original `ToolMessage` being evicted.
        replacement_text: The truncation notice and preview text.

    Returns:
        Replacement content: a string or a list of content blocks.
    """
    if isinstance(message.content, str):
        return replacement_text
    media_blocks = [block for block in message.content_blocks if block["type"] != "text"]
    if not media_blocks:
        # All content is text, so a plain string replacement is sufficient.
        return replacement_text
    return [cast("ContentBlock", {"type": "text", "text": replacement_text}), *media_blocks]


def _build_evicted_tool_message(message: ToolMessage, evicted_content: str | list[ContentBlock]) -> ToolMessage:
    """Build a replacement `ToolMessage` carrying `evicted_content`, preserving identity fields.

    Args:
        message: The original `ToolMessage` being evicted.
        evicted_content: Replacement content produced by `_build_evicted_content`.

    Returns:
        A new `ToolMessage` with the same `id`, `tool_call_id`, `name`, `artifact`,
            `status`, `additional_kwargs`, and `response_metadata` as the original.
    """
    return ToolMessage(
        content=cast("str | list[str | dict]", evicted_content),
        tool_call_id=message.tool_call_id,
        name=message.name,
        id=message.id,
        artifact=message.artifact,
        status=message.status,
        additional_kwargs=dict(message.additional_kwargs),
        response_metadata=dict(message.response_metadata),
    )


def _offload_tool_message_content(
    message: ToolMessage,
    content_str: str,
    backend: BackendProtocol,
    large_tool_results_prefix: str,
) -> ToolMessage | None:
    """Write `content_str` to `{prefix}/{tool_call_id}` and return a clipped replacement.

    The replacement carries a head+tail preview and the offload path in
    `TOO_LARGE_TOOL_MSG` format so the agent can `read_file` the full content
    by `tool_call_id`.

    Args:
        message: The `ToolMessage` whose content is being offloaded.
        content_str: The full text content to persist.
        backend: Backend to write the offloaded content to.
        large_tool_results_prefix: Path prefix for offloaded tool results.

    Returns:
        The clipped replacement `ToolMessage`, or `None` if the backend write
            failed — the caller should keep the original message in that case.
    """
    sanitized_id = sanitize_tool_call_id(message.tool_call_id) if message.tool_call_id else "unknown"
    file_path = f"{large_tool_results_prefix.rstrip('/')}/{sanitized_id}"
    try:
        result = backend.write(file_path, content_str)
    except Exception:
        return None
    if result is None or result.error:
        return None
    replacement_text = TOO_LARGE_TOOL_MSG.format(
        tool_call_id=message.tool_call_id,
        file_path=file_path,
        content_sample=_create_content_preview(content_str),
    )
    return _build_evicted_tool_message(message, _build_evicted_content(message, replacement_text))


async def _aoffload_tool_message_content(
    message: ToolMessage,
    content_str: str,
    backend: BackendProtocol,
    large_tool_results_prefix: str,
) -> ToolMessage | None:
    """Async variant of `_offload_tool_message_content` using `backend.awrite`.

    Args:
        message: The `ToolMessage` whose content is being offloaded.
        content_str: The full text content to persist.
        backend: Backend to write the offloaded content to.
        large_tool_results_prefix: Path prefix for offloaded tool results.

    Returns:
        The clipped replacement `ToolMessage`, or `None` if the backend write failed.
    """
    sanitized_id = sanitize_tool_call_id(message.tool_call_id) if message.tool_call_id else "unknown"
    file_path = f"{large_tool_results_prefix.rstrip('/')}/{sanitized_id}"
    try:
        result = await backend.awrite(file_path, content_str)
    except Exception:
        return None
    if result is None or result.error:
        return None
    replacement_text = TOO_LARGE_TOOL_MSG.format(
        tool_call_id=message.tool_call_id,
        file_path=file_path,
        content_sample=_create_content_preview(content_str),
    )
    return _build_evicted_tool_message(message, _build_evicted_content(message, replacement_text))
