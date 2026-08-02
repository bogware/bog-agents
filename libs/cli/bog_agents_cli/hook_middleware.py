"""PreToolUse hook enforcement middleware (hook-bus completion).

Wraps every tool call and runs the decision-capable `PreToolUse` hooks (from
`hook_decisions.py`). If a hook denies the call, the tool body never executes and
an error `ToolMessage` is returned in its place — the same fail-closed shape the
smart-approvals / expert-rules middleware use. Hooks themselves are fail-open (a
crashing/timing-out hook never blocks), so this can only *tighten* behaviour a
user opted into.

The middleware is added by `create_cli_agent` only when decision-capable hooks
exist (bog `hooks.json` entries on the `PreToolUse` event plus ingested
Claude/Cursor hook files), so agents with no hooks pay nothing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from bog_agents_cli.hook_decisions import HookDecision, evaluate_decision_hooks

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.tools.tool_node import ToolCallRequest
    from langgraph.types import Command


class PreToolUseHookMiddleware(AgentMiddleware):
    """Enforce `PreToolUse` decision hooks: a denying hook blocks the tool call."""

    def __init__(self, hooks: list[dict[str, Any]], *, timeout: float = 5.0) -> None:
        """Initialize with the decision-capable hooks to evaluate.

        Args:
            hooks: bog-format hook dicts (see `hook_decisions`).
            timeout: Per-hook timeout in seconds.
        """
        super().__init__()
        self._hooks = hooks
        self._timeout = timeout

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Run PreToolUse hooks; a deny fails closed, otherwise pass through."""
        decision = self._evaluate(request)
        if decision.blocks:
            return self._deny(request, decision)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Async variant — the (subprocess) hook evaluation runs off the loop."""
        decision = await asyncio.to_thread(self._evaluate, request)
        if decision.blocks:
            return self._deny(request, decision)
        return await handler(request)

    def _evaluate(self, request: ToolCallRequest) -> HookDecision:
        tool_call = request.tool_call or {}
        name = str(tool_call.get("name", ""))
        args = tool_call.get("args", {})
        return evaluate_decision_hooks(
            "PreToolUse",
            {"tool": name, "args": args},
            self._hooks,
            tool_name=name,
            timeout=self._timeout,
        )

    @staticmethod
    def _deny(request: ToolCallRequest, decision: HookDecision) -> ToolMessage:
        tool_call = request.tool_call or {}
        reason = decision.reason or "A PreToolUse hook denied this call."
        return ToolMessage(
            content=f"Blocked by a PreToolUse hook: {reason}",
            tool_call_id=str(tool_call.get("id", "")),
            name=str(tool_call.get("name", "")),
            status="error",
        )


__all__ = ["PreToolUseHookMiddleware"]
