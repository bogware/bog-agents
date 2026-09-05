"""PreToolUse / PostToolUse hook enforcement middleware (hook bus v2, ROADMAP #64).

Wraps every tool call and runs the decision-capable hooks from
`hook_decisions.py` and the prompt hooks from `prompt_hooks.py`:

* `PreToolUse` command hooks may deny (the tool body never runs; an error
  `ToolMessage` is returned), may fail with `on_failure: deny` (same), or with
  `on_failure: ask` (allowed here — the approval path already forced a prompt
  for the batch).
* `PreToolUse` prompt hooks are judged by a small model and are fail-closed.
* `PostToolUse` hooks may replace the tool result (`{"tool_result": "..."}`)
  before the `ToolMessage` reaches the model, or block it.

Command hooks themselves stay fail-open unless their `on_failure` says
otherwise. The middleware is added by `create_cli_agent` only when such hooks
exist, so hookless agents pay nothing.
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
    """Enforce PreToolUse / PostToolUse decision hooks and PreToolUse prompt hooks."""

    def __init__(
        self,
        hooks: list[dict[str, Any]],
        *,
        post_hooks: list[dict[str, Any]] | None = None,
        prompt_hooks: list[dict[str, Any]] | None = None,
        prompt_invoke: Callable[[str, str], str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Initialize with the hooks to evaluate.

        Args:
            hooks: `PreToolUse` command hooks (bog-format dicts).
            post_hooks: `PostToolUse` command hooks (result replacement / block).
            prompt_hooks: `PreToolUse` prompt hooks (`type: prompt`).
            prompt_invoke: `(system, user) -> str` judge for the prompt hooks;
                `None` denies every matching prompt hook (fail-closed).
            timeout: Per-hook timeout in seconds.
        """
        super().__init__()
        self._hooks = hooks
        self._post_hooks = list(post_hooks or [])
        self._prompt_hooks = list(prompt_hooks or [])
        self._prompt_invoke = prompt_invoke
        self._timeout = timeout

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Run PreToolUse hooks; a deny fails closed; PostToolUse hooks may rewrite the result."""
        decision = self._evaluate(request)
        if decision.blocks:
            return self._deny(request, decision)
        return self._after(request, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Async variant — the (subprocess / judge) evaluations run off the loop."""
        decision = await asyncio.to_thread(self._evaluate, request)
        if decision.blocks:
            return self._deny(request, decision)
        result = await handler(request)
        if self._post_hooks:
            return await asyncio.to_thread(self._after, request, result)
        return result

    # -- pre ------------------------------------------------------------------

    def _evaluate(self, request: ToolCallRequest) -> HookDecision:
        tool_call = request.tool_call or {}
        name = str(tool_call.get("name", ""))
        args = tool_call.get("args", {})
        payload = {"tool": name, "args": args}
        if self._hooks:
            decision = evaluate_decision_hooks(
                "PreToolUse",
                payload,
                self._hooks,
                tool_name=name,
                timeout=self._timeout,
            )
            if decision.blocks:
                return decision
        if self._prompt_hooks:
            from bog_agents_cli.prompt_hooks import evaluate_prompt_hooks

            verdict = evaluate_prompt_hooks(
                "PreToolUse",
                payload,
                self._prompt_hooks,
                invoke=self._prompt_invoke,
                tool_name=name,
            )
            if verdict.blocks:
                return verdict
        return HookDecision()

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

    # -- post -----------------------------------------------------------------

    def _after(
        self, request: ToolCallRequest, result: ToolMessage | Command[Any]
    ) -> ToolMessage | Command[Any]:
        """Apply PostToolUse hooks: replace the result text or block it."""
        if not self._post_hooks or not isinstance(result, ToolMessage):
            return result
        tool_call = request.tool_call or {}
        name = str(tool_call.get("name", ""))
        content = (
            result.content if isinstance(result.content, str) else str(result.content)
        )
        payload = {
            "tool": name,
            "args": tool_call.get("args", {}),
            "result": content[:10_000],
        }
        decision = evaluate_decision_hooks(
            "PostToolUse",
            payload,
            self._post_hooks,
            tool_name=name,
            timeout=self._timeout,
        )
        if decision.tool_result is not None:
            return result.model_copy(update={"content": decision.tool_result})
        if decision.blocks:
            reason = decision.reason or "A PostToolUse hook blocked this result."
            return result.model_copy(
                update={
                    "content": f"Blocked by a PostToolUse hook: {reason}",
                    "status": "error",
                }
            )
        return result


__all__ = ["PreToolUseHookMiddleware"]
