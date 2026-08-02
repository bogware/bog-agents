"""Auto-continue middleware for model responses cut off by the output-token limit.

When a chat model hits ``max_tokens`` (OpenAI ``finish_reason="length"``,
Anthropic ``stop_reason="max_tokens"``), the response is silently truncated
and the agent turn typically ends with a half-finished answer. This
middleware detects the truncation and transparently re-invokes the model
with the partial response plus a short "continue" instruction, then merges
the continuation into a single `AIMessage`.

Guarantees:

- Healing only applies to plain-text responses. Messages carrying tool
  calls, structured-output requests, or empty content are returned
  unchanged.
- The continuation loop is bounded (`max_continues`), so a model that
  keeps hitting the limit cannot spin forever.
- Re-invoking the inner handler re-runs only the raw model call (this
  middleware is placed innermost of the summarization stack), so a
  continuation never re-triggers compaction, argument truncation, or
  prompt-caching wrappers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest

_CONTINUE_PROMPT = (
    "[The previous response was cut off because the output token limit was reached. "
    "Continue exactly where you left off. Do not repeat or recap what was already "
    "written, and do not add new commentary.]"
)

# `finish_reason` / `stop_reason` values that indicate output-token truncation.
_TRUNCATION_REASONS: frozenset[str] = frozenset({"length", "max_tokens", "max_length", "maximum_output_tokens"})

_DEFAULT_MAX_CONTINUES = 2


class OutputTruncationMiddleware(AgentMiddleware[Any, Any, Any]):
    """Continue model responses that were truncated at the output-token limit.

    Args:
        max_continues: Maximum number of continuation re-invocations per
            truncated response before giving up and returning the merged
            (still-truncated) content.
        continue_prompt: Instruction appended after the partial response
            to drive the continuation call.

    Example:
        ```python
        from bog_agents.middleware import OutputTruncationMiddleware

        agent = create_agent(model="...", middleware=[OutputTruncationMiddleware()])
        ```
    """

    def __init__(
        self,
        *,
        max_continues: int = _DEFAULT_MAX_CONTINUES,
        continue_prompt: str = _CONTINUE_PROMPT,
    ) -> None:
        super().__init__()
        if max_continues < 1:
            msg = f"max_continues must be >= 1, got {max_continues}"
            raise ValueError(msg)
        self._max_continues = max_continues
        self._continue_prompt = continue_prompt

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Invoke the model and auto-continue a truncated text response.

        Args:
            request: The model request to process.
            handler: The inner handler (the raw model call).

        Returns:
            The model response, merged when a truncation was healed, otherwise
                unchanged.
        """
        return self._heal(request, handler)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Any],
    ) -> ModelResponse[Any]:
        """Async twin of :meth:`wrap_model_call`."""
        return await self._aheal(request, handler)

    def _heal(self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], ModelResponse[Any]]) -> ModelResponse[Any]:
        """Sync healing loop: continue up to ``max_continues`` truncated text responses."""
        raw = handler(request)
        response = self._as_model_response(raw)
        if not self._can_heal(request, response):
            return self._passthrough(raw, response)
        original = cast("AIMessage", response.result[0])
        merged_text = _content_text(original.content)
        if merged_text is None:
            return self._passthrough(raw, response)
        parts = [original]
        for _ in range(self._max_continues):
            continuation = self._as_model_response(handler(self._continue_request(request, parts, merged_text)))
            if not self._can_heal(request, continuation):
                if self._is_plain_text(continuation):
                    tail = cast("AIMessage", continuation.result[0])
                    parts.append(tail)
                    merged_text = f"{merged_text}\n{_content_text(tail.content)}"
                return self._merged_response(original, merged_text, parts[-1], parts)
            next_aim = cast("AIMessage", continuation.result[0])
            next_text = _content_text(next_aim.content)
            if next_text is None:
                return self._merged_response(original, merged_text, parts[-1], parts)
            parts.append(next_aim)
            merged_text = f"{merged_text}\n{next_text}"
        # Exhausted the continuation budget; return whatever accumulated.
        return self._merged_response(original, merged_text, parts[-1], parts)

    async def _aheal(self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]) -> ModelResponse[Any]:
        """Async healing loop; mirrors :meth:`_heal`."""
        raw = await handler(request)
        response = self._as_model_response(raw)
        if not self._can_heal(request, response):
            return self._passthrough(raw, response)
        original = cast("AIMessage", response.result[0])
        merged_text = _content_text(original.content)
        if merged_text is None:
            return self._passthrough(raw, response)
        parts = [original]
        for _ in range(self._max_continues):
            continuation = self._as_model_response(await handler(self._continue_request(request, parts, merged_text)))
            if not self._can_heal(request, continuation):
                if self._is_plain_text(continuation):
                    tail = cast("AIMessage", continuation.result[0])
                    parts.append(tail)
                    merged_text = f"{merged_text}\n{_content_text(tail.content)}"
                return self._merged_response(original, merged_text, parts[-1], parts)
            next_aim = cast("AIMessage", continuation.result[0])
            next_text = _content_text(next_aim.content)
            if next_text is None:
                return self._merged_response(original, merged_text, parts[-1], parts)
            parts.append(next_aim)
            merged_text = f"{merged_text}\n{next_text}"
        return self._merged_response(original, merged_text, parts[-1], parts)

    def _continue_request(self, request: ModelRequest[Any], parts: list[AIMessage], merged_text: str) -> ModelRequest[Any]:
        """Build the continuation request.

        The model must see everything it has written so far, otherwise the
        second and later continuations resume from only the most recent chunk
        and repeat earlier content. Providers reject consecutive assistant
        turns, so the accumulated text is collapsed into one `AIMessage`; the
        first continuation reuses the original message untouched.

        Args:
            request: The original model request.
            parts: Partial responses received so far.
            merged_text: The concatenated text of `parts`.

        Returns:
            A request whose messages end with the partial answer and the
                continue instruction.
        """
        partial = parts[0] if len(parts) == 1 else AIMessage(content=merged_text)
        return request.override(messages=[*request.messages, partial, HumanMessage(content=self._continue_prompt)])

    @staticmethod
    def _passthrough(raw: Any, normalized: ModelResponse[Any]) -> ModelResponse[Any]:
        """Return an untouched response without discarding a wrapper.

        `_as_model_response` unwraps an `ExtendedModelResponse` to inspect its
        messages, but that wrapper may carry a state update (a middleware
        running inside this one could contribute one). Returning the unwrapped
        value would silently drop it, so hand back the original object when
        nothing was healed.

        Args:
            raw: Whatever the inner handler returned.
            normalized: The `ModelResponse` view of `raw`.

        Returns:
            `raw` when it wraps a `ModelResponse`, else the normalized value.
        """
        if hasattr(raw, "model_response"):
            return cast("ModelResponse[Any]", raw)
        return normalized

    @staticmethod
    def _as_model_response(response: Any) -> ModelResponse[Any]:
        """Normalize the handler return into a `ModelResponse`."""
        if isinstance(response, AIMessage):
            return ModelResponse(result=[response])  # type: ignore[call-arg]
        if hasattr(response, "model_response"):  # ExtendedModelResponse
            return response.model_response  # type: ignore[no-any-return]
        return response

    def _can_heal(self, request: ModelRequest[Any], response: ModelResponse[Any]) -> bool:
        """Return whether the response is a healable truncated text reply."""
        if request.response_format is not None or response.structured_response is not None:
            return False
        if not response.result or not isinstance(response.result[0], AIMessage):
            return False
        aim = response.result[0]
        if aim.tool_calls:
            return False
        return _is_truncated(aim)

    @staticmethod
    def _is_plain_text(response: ModelResponse[Any]) -> bool:
        """Return whether the response is a single text-only `AIMessage`."""
        if not response.result or not isinstance(response.result[0], AIMessage):
            return False
        aim = response.result[0]
        return not aim.tool_calls and _content_text(aim.content) is not None

    @staticmethod
    def _merged_response(partial: AIMessage, merged_text: str, final: AIMessage, parts: list[AIMessage]) -> ModelResponse[Any]:
        """Merge partial + continuation text into a single `AIMessage`.

        Args:
            partial: The first (truncated) message; its `id` is preserved.
            merged_text: The combined text content.
            final: The last message; its metadata reflects the terminal state.
            parts: Every message that contributed, whose token usage is summed
                onto the merged message.

        Returns:
            A `ModelResponse` wrapping the merged message.
        """
        merged = AIMessage(
            content=merged_text,
            id=partial.id,
            additional_kwargs=dict(final.additional_kwargs),
            response_metadata=dict(final.response_metadata),
            tool_calls=[],
            usage_metadata=_sum_usage(parts),
        )
        return ModelResponse(result=[merged])  # type: ignore[call-arg]


def _sum_usage(parts: list[AIMessage]) -> dict[str, Any] | None:
    """Sum `usage_metadata` across every message that formed a merged reply.

    A healed turn costs the sum of its calls — each continuation re-sends the
    prompt and is billed for it. Without this the merged message carries no
    usage at all and `CostTrackerMiddleware`, which wraps this middleware,
    records nothing for the turn, silently defeating `budget_usd` caps.

    Args:
        parts: The partial and continuation messages.

    Returns:
        The summed usage mapping, or `None` when no part reported usage.
    """
    total: dict[str, Any] = {}
    details_total: dict[str, int] = {}
    seen = False
    for message in parts:
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, dict):
            continue
        seen = True
        for key, value in usage.items():
            if key == "input_token_details":
                if isinstance(value, dict):
                    for detail_key, detail_value in value.items():
                        if isinstance(detail_value, int):
                            details_total[detail_key] = details_total.get(detail_key, 0) + detail_value
            elif isinstance(value, int):
                total[key] = total.get(key, 0) + value
    if not seen:
        return None
    if details_total:
        total["input_token_details"] = details_total
    return total


def _content_text(content: Any) -> str | None:
    """Extract plain text from `AIMessage.content` (str or content blocks).

    Args:
        content: The `AIMessage.content` value.

    Returns:
        The concatenated text blocks, or `None` when no text is present or
            the content shape is unsupported.
    """
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if not parts:
            return None
        return "\n".join(parts)
    return None


def _is_truncated(aim: AIMessage) -> bool:
    """Return whether an `AIMessage` was cut off at the output-token limit.

    Args:
        aim: The model message to inspect.

    Returns:
        `True` when `finish_reason` / `stop_reason` metadata indicates
            output-token truncation.
    """
    for source in (aim.response_metadata, aim.additional_kwargs):
        if not isinstance(source, dict):
            continue
        for key in ("finish_reason", "stop_reason"):
            reason = source.get(key)
            if isinstance(reason, str) and reason in _TRUNCATION_REASONS:
                return True
    return False


__all__ = ["OutputTruncationMiddleware"]
