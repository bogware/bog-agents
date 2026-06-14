"""GuardrailMiddleware — run input/output guardrails around the model call (#18).

Composes a list of :class:`~bog_agents.guardrails.core.Guardrail`s into the
agent: input guardrails validate the latest user message before the model
runs; output guardrails validate the model's response. A tripped guardrail
raises :class:`~bog_agents.guardrails.core.GuardrailTripwireError` (fail-fast).
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

from bog_agents.guardrails.core import (
    GuardrailTripwireError,
    first_tripped,
    run_guardrails,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from bog_agents.guardrails.core import Guardrail

logger = logging.getLogger(__name__)


def _text_of(content: Any) -> str:
    """Flatten message content (string or Anthropic block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content) if content is not None else ""


def _last_human_text(request: ModelRequest[ContextT]) -> str:
    for msg in reversed(list(getattr(request, "messages", []) or [])):
        if getattr(msg, "type", "") == "human":
            return _text_of(getattr(msg, "content", ""))
    return ""


def _response_text(response: ModelResponse[ResponseT]) -> str:
    parts = [
        _text_of(getattr(m, "content", "")) for m in (getattr(response, "result", None) or [])
    ]
    return "\n".join(p for p in parts if p)


class GuardrailMiddleware(AgentMiddleware[AgentState, ContextT, ResponseT]):
    """Run input/output guardrails around each model call, failing fast on a trip."""

    def __init__(
        self,
        *,
        input_guardrails: Sequence[Guardrail] | None = None,
        output_guardrails: Sequence[Guardrail] | None = None,
    ) -> None:
        """Initialize.

        Args:
            input_guardrails: Guardrails run on the latest user message before
                the model is called.
            output_guardrails: Guardrails run on the model's response.
        """
        super().__init__()
        self.input_guardrails = list(input_guardrails or [])
        self.output_guardrails = list(output_guardrails or [])

    def _check_sync(self, text: str, guardrails: list[Guardrail], stage: str) -> None:
        for g in guardrails:
            result = g.check(text)
            if inspect.isawaitable(result):
                with contextlib.suppress(Exception):
                    result.close()  # type: ignore[union-attr]
                logger.debug(
                    "guardrail %s is async-only; skipped on the sync path",
                    getattr(g, "name", "?"),
                )
                continue
            if result.tripped:
                raise GuardrailTripwireError(result, stage=stage)

    async def _check_async(self, text: str, guardrails: list[Guardrail], stage: str) -> None:
        tripped = first_tripped(await run_guardrails(text, guardrails, stop_on_first=True))
        if tripped is not None:
            raise GuardrailTripwireError(tripped, stage=stage)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Validate input, call the model, validate output (sync)."""
        if self.input_guardrails:
            self._check_sync(_last_human_text(request), self.input_guardrails, "input")
        response = handler(request)
        if self.output_guardrails:
            self._check_sync(_response_text(response), self.output_guardrails, "output")
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Validate input, call the model, validate output (async)."""
        if self.input_guardrails:
            await self._check_async(_last_human_text(request), self.input_guardrails, "input")
        response = await handler(request)
        if self.output_guardrails:
            await self._check_async(_response_text(response), self.output_guardrails, "output")
        return response
