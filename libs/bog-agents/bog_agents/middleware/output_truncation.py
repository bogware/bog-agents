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
        response = self._as_model_response(handler(request))
        if not self._can_heal(request, response):
            return response
        aim = cast("AIMessage", response.result[0])
        merged_text = _content_text(aim.content)
        if merged_text is None:
            return response
        for _ in range(self._max_continues):
            continuation = self._as_model_response(
                handler(request.override(messages=[*request.messages, aim, HumanMessage(content=self._continue_prompt)]))
            )
            if not self._can_heal(request, continuation):
                if self._is_plain_text(continuation):
                    merged_text = f"{merged_text}\n{_content_text(continuation.result[0].content)}"
                    return self._merged_response(aim, merged_text, continuation.result[0])
                return response
            next_aim = cast("AIMessage", continuation.result[0])
            next_text = _content_text(next_aim.content)
            if next_text is None:
                return response
            merged_text = f"{merged_text}\n{next_text}"
            aim = next_aim
        # Exhausted the continuation budget; return whatever accumulated.
        return self._merged_response(cast("AIMessage", response.result[0]), merged_text, aim)

    async def _aheal(self, request: ModelRequest[Any], handler: Callable[[ModelRequest[Any]], Any]) -> ModelResponse[Any]:
        """Async healing loop; mirrors :meth:`_heal`."""
        response = self._as_model_response(await handler(request))
        if not self._can_heal(request, response):
            return response
        aim = cast("AIMessage", response.result[0])
        merged_text = _content_text(aim.content)
        if merged_text is None:
            return response
        for _ in range(self._max_continues):
            continuation = self._as_model_response(
                await handler(request.override(messages=[*request.messages, aim, HumanMessage(content=self._continue_prompt)]))
            )
            if not self._can_heal(request, continuation):
                if self._is_plain_text(continuation):
                    merged_text = f"{merged_text}\n{_content_text(continuation.result[0].content)}"
                    return self._merged_response(aim, merged_text, continuation.result[0])
                return response
            next_aim = cast("AIMessage", continuation.result[0])
            next_text = _content_text(next_aim.content)
            if next_text is None:
                return response
            merged_text = f"{merged_text}\n{next_text}"
            aim = next_aim
        return self._merged_response(cast("AIMessage", response.result[0]), merged_text, aim)

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
    def _merged_response(partial: AIMessage, merged_text: str, final: AIMessage) -> ModelResponse[Any]:
        """Merge partial + continuation text into a single `AIMessage`.

        Args:
            partial: The first (truncated) message; its `id` is preserved.
            merged_text: The combined text content.
            final: The last message; its metadata reflects the terminal state.

        Returns:
            A `ModelResponse` wrapping the merged message.
        """
        merged = AIMessage(
            content=merged_text,
            id=partial.id,
            additional_kwargs=dict(final.additional_kwargs),
            response_metadata=dict(final.response_metadata),
            tool_calls=[],
        )
        return ModelResponse(result=[merged])  # type: ignore[call-arg]


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
