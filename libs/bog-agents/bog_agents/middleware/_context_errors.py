"""Classification of context-length / token-limit model errors.

Providers reject an oversized request in a few overlapping ways, and
none of them is guaranteed across the SDK's provider surface:

- LangChain's own detection: ``ContextOverflowError``.
- OpenAI-style HTTP 400 with a body like ``This model's maximum
  context length is 200000 tokens...``.
- Anthropic-style HTTP 400 with ``prompt is too long: 200000 tokens
  max, 260000 tokens requested``.
- litellm's ``ContextWindowExceededError`` (no ``status_code``).

``is_context_length_error`` buckets these into a single predicate so
``SummarizationMiddleware`` can treat a provider-side rejection the
same way it treats an in-framework ``ContextOverflowError``: compact
the history and retry instead of ending the turn.

The predicate is deliberately conservative. A false positive at worst
wastes one compaction attempt (the compacted retry is not wrapped, so
a genuinely unrelated error re-surfaces on retry); a false negative
lets the original error propagate unchanged.
"""

from __future__ import annotations

from langchain_core.exceptions import ContextOverflowError

# Phrases distinctive enough to trust without an HTTP status code.
# Lower-cased before matching.
_STRONG_MARKERS: tuple[str, ...] = (
    "prompt is too long",
    "prompt too long",
    "context_length_exceeded",
    "context length exceeded",
    "maximum context length",
    "exceeds the maximum context",
    "context window is too",
    "context window is smaller than",
    "too many tokens",
    "input_length_exceeded",
    "input too long",
    "maximum token limit",
)

# Broader phrases; only trusted when the exception also carries a
# 4xx status code so we don't misfire on unrelated "context" prose.
_WEAK_MARKERS: tuple[str, ...] = (
    "context window",
    "context length",
    "maximum context",
    "max context",
    "token limit",
)

# HTTP statuses that commonly carry provider context-length rejections.
_CLIENT_ERROR_STATUSES: frozenset[int] = frozenset({400, 413, 422, 429})

# Exception class names that identify the condition even without a
# message or status (litellm, Bedrock, etc.), lower-cased.
_NAME_SUBSTRINGS: tuple[str, ...] = (
    "contextexceeded",
    "contextwindowexceeded",
    "contextlengthexceeded",
    "maximumcontextlengthexceeded",
    "contexttoolong",
    "tokenlimitexceeded",
    "maximumtokens",
)


def is_context_length_error(exc: BaseException) -> bool:
    """Return `True` when ``exc`` indicates the input exceeded the context window.

    Args:
        exc: The exception raised by the model call.

    Returns:
        `True` for `ContextOverflowError`, provider HTTP 400/429
        context-length rejections, and provider exceptions whose class
        name or message identifies a context-length condition. `False`
        for everything else (including all transient/network errors, so
        this never hijacks a retryable failure).
    """
    if isinstance(exc, ContextOverflowError):
        return True

    name = type(exc).__name__.lower()
    if any(marker in name for marker in _NAME_SUBSTRINGS):
        return True

    body = str(exc).lower()
    if any(marker in body for marker in _STRONG_MARKERS):
        return True

    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _CLIENT_ERROR_STATUSES:
        return any(marker in body for marker in _WEAK_MARKERS)

    return False


__all__ = ["is_context_length_error"]
