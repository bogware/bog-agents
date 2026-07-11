"""Middleware to patch dangling tool calls in the messages history."""

from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

__all__ = [
    "PatchToolCallsMiddleware",
]


class PatchToolCallsMiddleware(AgentMiddleware):
    """Middleware to patch dangling tool calls in the messages history.

    A dangling tool call is a `tool_call` (or an `invalid_tool_call`, produced when
    the model emits malformed/truncated arguments) on an `AIMessage` that has no
    matching `ToolMessage`. Providers reject a request whose `tool_use` block is
    unanswered, so every dangling call gets a synthetic `ToolMessage` before the
    agent runs.
    """

    def before_agent(self, state: AgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Before the agent runs, answer dangling tool calls from any `AIMessage`.

        Args:
            state: Current agent state.
            runtime: Runtime context (unused).

        Returns:
            A state update overwriting `messages` with the patched history, or
            `None` when nothing is dangling (the common case — rewriting an
            unchanged history every turn is pure churn).
        """
        messages = state["messages"]
        if not messages:
            return None

        answered_ids = {msg.tool_call_id for msg in messages if msg.type == "tool"}  # ty: ignore[unresolved-attribute]

        if not any(
            tool_call["id"] is not None and tool_call["id"] not in answered_ids
            for msg in messages
            if isinstance(msg, AIMessage)
            for tool_call in (*msg.tool_calls, *msg.invalid_tool_calls)
        ):
            return None

        patched_messages: list[AnyMessage] = []
        for msg in messages:
            patched_messages.append(msg)
            if not isinstance(msg, AIMessage):
                continue
            for tool_call in (*msg.tool_calls, *msg.invalid_tool_calls):
                tool_call_id = tool_call["id"]
                if tool_call_id is None or tool_call_id in answered_ids:
                    continue
                name = tool_call["name"] or "unknown"
                if tool_call.get("type") == "invalid_tool_call":
                    content = f"Tool call {name} with id {tool_call_id} could not be executed - arguments were malformed or truncated."
                else:
                    content = f"Tool call {name} with id {tool_call_id} was cancelled - another message came in before it could be completed."
                patched_messages.append(ToolMessage(content=content, name=name, tool_call_id=tool_call_id))

        return {"messages": Overwrite(patched_messages)}
