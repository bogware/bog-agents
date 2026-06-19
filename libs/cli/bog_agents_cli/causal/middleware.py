"""CausalMiddleware — records causal events during an agent run (M2).

Wires :class:`~bog_agents_cli.causal.ledger.CausalLedger` into the
LangChain middleware stack. Each tool call and model call becomes one
or more events; the parent-id linkage threads them into a causal graph
the user can walk after the fact with ``/causal why <id>``.

Wiring rules
------------

* The most recent ``USER_MESSAGE`` event is the parent of the next
  ``MODEL_CALL``. After that, model calls and tool calls form a chain
  where each link's parent is the previous link.
* When a tool returns, the ``TOOL_RESULT`` event lists the matching
  ``TOOL_CALL`` as its parent so renderers can pair them.
* A final assistant turn (a model_call that produced no further tool
  calls) is annotated with ``FINAL_ANSWER`` to give the renderer a
  natural stopping point.

The middleware is **opt-in**. It only records when constructed with
``enabled=True`` (the default is True at construction, but the CLI
wires it off by default until the user runs ``/causal on``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from typing_extensions import TypedDict

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    CausalLedger,
    EventKind,
)

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import ToolMessage
    from langgraph.prebuilt.tool_node import ToolCallRequest

logger = logging.getLogger(__name__)


_TOOL_ARG_PREVIEW_LIMIT = 200
_TOOL_RESULT_PREVIEW_LIMIT = 200


class CausalState(TypedDict, total=False):
    """No agent-state contributions today; reserved for future use."""

    causal_session_id: str


class CausalMiddleware(AgentMiddleware[CausalState, ContextT, ResponseT]):
    """Record causal events into a :class:`CausalLedger`.

    Args:
        ledger: A live :class:`CausalLedger`. The caller owns its
            lifetime — the middleware never closes it.
        enabled: When False, all hooks short-circuit to ``call_next``
            with zero overhead. The CLI flips this on via the
            ``/causal on`` slash command.
        actor_label: Logical name shown in the renderer for events
            this middleware contributes (e.g. the active model spec).
            When empty, falls back to ``"agent"``.
    """

    def __init__(
        self,
        *,
        ledger: CausalLedger,
        enabled: bool = True,
        actor_label: str = "",
    ) -> None:
        super().__init__()
        self._ledger = ledger
        self._enabled = enabled
        self._actor = actor_label or "agent"
        # Per-turn parent tracking. The TUI feeds in one user prompt
        # at a time; this is the id of the most recent event so the
        # next model/tool call hangs off the right ancestor. We
        # update it after every record() so chains form naturally.
        self._head_id: int | None = None
        # tool_call_id (LangChain identifier on a tool message) →
        # ledger event id. Lets us pair TOOL_RESULT events back to
        # their originating TOOL_CALL.
        self._tool_call_to_event: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def ledger(self) -> CausalLedger:
        return self._ledger

    def set_enabled(self, on: bool) -> None:
        """Toggle recording on/off without rebuilding the agent."""
        self._enabled = on

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record_user_message(self, content: str) -> CausalEvent:
        """Anchor a new causal chain with a USER_MESSAGE event.

        Called by the CLI when the user submits a prompt. The returned
        event becomes the parent of the upcoming model call.
        """
        event = self._ledger.record(
            EventKind.USER_MESSAGE,
            actor="user",
            summary=content[:240],
            payload={"length": len(content)},
        )
        self._head_id = event.id
        return event

    def record_rule_fire(
        self,
        *,
        rule_name: str,
        action: str,
        detail: str = "",
    ) -> CausalEvent | None:
        """Record an expert-rule firing.

        Returns the new event id so the rule engine can thread its
        cascades. Returns ``None`` when disabled to make the call
        site simpler.
        """
        if not self._enabled:
            return None
        parents = (self._head_id,) if self._head_id else ()
        event = self._ledger.record(
            EventKind.RULE_FIRE,
            actor=rule_name,
            summary=f"{action}: {detail}" if detail else action,
            parent_ids=parents,
            payload={"action": action, "detail": detail},
        )
        # Rules don't reset head — they annotate the current chain.
        return event

    # ------------------------------------------------------------------
    # Middleware hooks
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        if not self._enabled:
            return call_next(request)
        parents = (self._head_id,) if self._head_id else ()
        event = self._ledger.record(
            EventKind.MODEL_CALL,
            actor=self._actor,
            summary=f"model invoked ({len(request.messages)} msgs in context)",
            parent_ids=parents,
            payload={"message_count": len(request.messages)},
        )
        self._head_id = event.id
        response = call_next(request)
        self._post_model_call(response, model_event_id=event.id)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        if not self._enabled:
            return await call_next(request)
        parents = (self._head_id,) if self._head_id else ()
        event = self._ledger.record(
            EventKind.MODEL_CALL,
            actor=self._actor,
            summary=f"model invoked ({len(request.messages)} msgs in context)",
            parent_ids=parents,
            payload={"message_count": len(request.messages)},
        )
        self._head_id = event.id
        response = await call_next(request)
        self._post_model_call(response, model_event_id=event.id)
        return response

    def _post_model_call(self, response: Any, *, model_event_id: int) -> None:
        """Inspect the response and emit TOOL_CALL or FINAL_ANSWER."""
        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name", "<unknown>")
                args_preview = self._preview_args(tc.get("args", {}))
                tc_id = tc.get("id", "")
                event = self._ledger.record(
                    EventKind.TOOL_CALL,
                    actor=name,
                    summary=args_preview,
                    parent_ids=(model_event_id,),
                    payload={
                        "tool_call_id": tc_id,
                        "args_keys": sorted((tc.get("args") or {}).keys()),
                    },
                )
                if tc_id:
                    self._tool_call_to_event[tc_id] = event.id
            return
        # No tool calls → this is a terminal assistant turn.
        self._ledger.record(
            EventKind.FINAL_ANSWER,
            actor=self._actor,
            summary=self._preview_content(response),
            parent_ids=(model_event_id,),
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        if not self._enabled:
            return handler(request)
        result = handler(request)
        self._post_tool_call(request, result)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        if not self._enabled:
            return await handler(request)
        result = await handler(request)
        self._post_tool_call(request, result)
        return result

    def _post_tool_call(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Any,
    ) -> None:
        """Record TOOL_RESULT, linking back to the originating TOOL_CALL."""
        tool_name = getattr(getattr(request, "tool_call", None), "name", "")
        if not tool_name:
            tool_name = getattr(request, "name", "") or "<tool>"
        tool_call_id = ""
        tc = getattr(request, "tool_call", None)
        if tc is not None:
            tool_call_id = (
                tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            )
        parent_event = self._tool_call_to_event.get(tool_call_id, self._head_id)
        parents: tuple[int, ...] = (parent_event,) if parent_event else ()
        summary = self._preview_content(result)
        event = self._ledger.record(
            EventKind.TOOL_RESULT,
            actor=tool_name,
            summary=summary,
            parent_ids=parents,
            payload={
                "tool_call_id": tool_call_id,
                "is_error": bool(getattr(result, "status", "") == "error"),
            },
        )
        self._head_id = event.id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _preview_args(args: dict[str, Any] | Any) -> str:
        if not args:
            return "(no args)"
        if not isinstance(args, dict):
            return str(args)[:_TOOL_ARG_PREVIEW_LIMIT]
        parts: list[str] = []
        for k, v in args.items():
            text = repr(v)
            if len(text) > 60:
                text = text[:59] + "…"
            parts.append(f"{k}={text}")
        joined = " ".join(parts)
        if len(joined) > _TOOL_ARG_PREVIEW_LIMIT:
            joined = joined[: _TOOL_ARG_PREVIEW_LIMIT - 1] + "…"
        return joined

    @staticmethod
    def _preview_content(obj: Any) -> str:
        content = getattr(obj, "content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif isinstance(p, str):
                    parts.append(p)
            content = "".join(parts)
        text = str(content or "")[:_TOOL_RESULT_PREVIEW_LIMIT]
        return text.replace("\n", " ").strip()


__all__ = [
    "CausalMiddleware",
    "CausalState",
]
