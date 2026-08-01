"""Deferred tool schemas — keep large tool definitions out of model context.

Every model call re-sends the JSON schema for every bound tool, so tools
with big schemas (or many tools) inflate input tokens on every turn.
``DeferredToolsMiddleware`` hides the schemas of selected tools from the
model until it needs them:

1. A deferred tool's schema is *not* shown to the model (no token cost).
2. A ``tool_search`` metatool lets the model find tools by name, description,
   or caller-supplied keywords.
3. A ``select`` metatool (also accepted as a ``select:<name>`` call) activates
   a tool and returns its full JSON schema; once activated, the real tool is
   included in every subsequent model request so it can be called directly.

Activation state is per-middleware-instance, so each agent gets its own
activation set. The registry of available tools is populated lazily from the
first model request, which means tools injected by *other* middleware
(filesystem, subagent, todo list, …) are deferred correctly even though they
were never passed to ``create_agent(tools=...)``.

Wire it up via ``FeatureConfig``::

    config = FeatureConfig(
        enable_deferred_tools=True,
        deferred_tools=["git_diff", "git_log"],
    )
    agent = create_agent(model=..., tools=[*git_tools_bundle(...)], config=config)

The deferred tools stay bound to the compiled graph (the ``ToolNode`` can
still execute them once activated); only the schema the model sees is hidden.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from bog_agents.tools.coercion import SemanticNumber

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse, ToolCallRequest


def _tool_name(tool: BaseTool | dict[str, Any]) -> str | None:
    """Extract the tool name from a `BaseTool` or dict tool."""
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class DeferredToolsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Hide selected tool schemas from the model until they are activated.

    Args:
        deferred_names: Names of tools whose schemas should be hidden from the
            model until a `select` call activates them. Tools must be bound to
            the agent (via `tools=` or another middleware); their real schema
            is resolved lazily from the first model request.
        keywords: Optional name → keyword list used to enrich `tool_search`
            matching beyond the tool's own name and description.
    """

    def __init__(
        self,
        *,
        deferred_names: frozenset[str] = frozenset(),
        keywords: dict[str, list[str]] | None = None,
    ) -> None:
        self._deferred_names = frozenset(deferred_names)
        self._extra_keywords = {name: tuple(words) for name, words in (keywords or {}).items()}
        self._activated: set[str] = set()
        self._registry: dict[str, BaseTool] = {}
        self.tools = self._build_metatools()

    # ------------------------------------------------------------------
    # Metatools
    # ------------------------------------------------------------------

    def _build_metatools(self) -> list[BaseTool]:
        """Build the `tool_search` and `select` metatools (empty when idle)."""
        if not self._deferred_names:
            return []

        def tool_search(runtime: ToolRuntime[None, Any], query: str, limit: SemanticNumber = 10) -> str:
            """Find tools by name, description, or keyword. Useful when you need a tool you cannot see — many tools are deferred (hidden) to save context until selected. Pass a query describing what you want to do, then call select(name='<tool>') on a result to load its schema and make it callable."""
            del runtime
            return self._search(query, int(limit))

        def select(runtime: ToolRuntime[None, Any], name: str) -> str:
            """Activate a deferred tool so you can call it directly. The tool's full schema is returned; after this, the tool appears in your available tools. You may also call it as select:<name>. Use tool_search to discover tool names."""
            del runtime
            return self._activate(name)

        return [
            StructuredTool.from_function(
                name="tool_search",
                description=(
                    "Find tools by name, description, or keyword. Many tools are "
                    "deferred (their schemas are hidden to save context) until "
                    "activated. Call select(name='<tool>') on a result to load its "
                    "schema and make it callable."
                ),
                func=tool_search,
            ),
            StructuredTool.from_function(
                name="select",
                description=(
                    "Activate a deferred tool so it can be called directly. Returns "
                    "the tool's full schema; afterwards the tool is available on every "
                    "turn. The call may also be written as select:<name>."
                ),
                func=select,
            ),
        ]

    # ------------------------------------------------------------------
    # Request shaping
    # ------------------------------------------------------------------

    def _ensure_registry(self, request: ModelRequest[Any]) -> None:
        """Populate the tool registry from the first fully assembled request."""
        if self._registry:
            return
        for tool in request.tools:
            name = _tool_name(tool)
            if name is not None and name not in self._registry:
                self._registry[name] = tool

    def _visible_tools(self, request: ModelRequest[Any]) -> list[BaseTool | dict[str, Any]]:
        """Filter `request.tools` down to the set the model may call.

        Deferred-but-not-activated tools are dropped; everything else (visible
        tools, activated deferred tools, and the metatools) is kept in order.
        """
        visible: list[BaseTool | dict[str, Any]] = []
        seen: set[str] = set()
        for tool in request.tools:
            name = _tool_name(tool)
            if name is None:
                visible.append(tool)
                continue
            if name in self._deferred_names and name not in self._activated:
                continue
            if name in seen:
                continue
            seen.add(name)
            visible.append(tool)
        return visible

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Hide deferred tool schemas from the model before it is called."""
        if not self._deferred_names:
            return handler(request)
        self._ensure_registry(request)
        return handler(request.override(tools=self._visible_tools(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Async variant of `wrap_model_call`."""
        if not self._deferred_names:
            return await handler(request)
        self._ensure_registry(request)
        return await handler(request.override(tools=self._visible_tools(request)))

    # ------------------------------------------------------------------
    # Tool-call interception
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """Intercept `select:<name>` calls and route them to activation."""
        if not self._deferred_names:
            return handler(request)
        content = self._intercept_select_call(request)
        if content is not None:
            return self._select_message(request, content)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """Async variant of `wrap_tool_call`."""
        if not self._deferred_names:
            return await handler(request)
        content = self._intercept_select_call(request)
        if content is not None:
            return self._select_message(request, content)
        return await handler(request)

    def _intercept_select_call(self, request: ToolCallRequest) -> str | None:
        """Return activation text when the call is the `select:<name>` form.

        The native `select` metatool handles ordinary `select(name=...)` calls
        through the framework; this only catches the `select:<name>` shorthand,
        which is not a registered tool name.
        """
        call = request.tool_call
        name = call.get("name") if hasattr(call, "get") else None
        if not isinstance(name, str) or not name.startswith("select:"):
            return None
        target = name.split(":", 1)[1].strip()
        return self._activate(target)

    @staticmethod
    def _select_message(request: ToolCallRequest, content: str) -> ToolMessage:
        """Build a `ToolMessage` for a short-circuited `select:<name>` call."""
        call = request.tool_call
        return ToolMessage(
            content=content,
            tool_call_id=str(call.get("id", "") if hasattr(call, "get") else ""),
            name=str(call.get("name", "") if hasattr(call, "get") else ""),
            status="success",
        )

    # ------------------------------------------------------------------
    # Registry operations
    # ------------------------------------------------------------------

    def _search(self, query: str, limit: int) -> str:
        """Implement the `tool_search` metatool."""
        if not self._registry:
            return "No tools are available to search."
        terms = [t for t in re.split(r"\W+", query.lower()) if t] or [""]

        def matches(tool: BaseTool) -> bool:
            name = tool.name.lower()
            description = (tool.description or "").lower()
            extra = " ".join(self._extra_keywords.get(tool.name, ())).lower()
            return any(term in name or term in description or term in extra for term in terms)

        hits = [tool for tool in self._registry.values() if matches(tool)]
        hits.sort(key=lambda tool: tool.name)
        if not hits:
            available = ", ".join(sorted(self._registry)) or "none"
            return (
                f"No tools matched {query!r}. Available tools: {available}. "
                "Try a different query, or activate a tool directly with "
                "select(name='<tool>')."
            )
        lines = []
        for tool in hits[:limit]:
            state = "deferred" if tool.name in self._deferred_names else "always-visible"
            if tool.name in self._activated:
                state = "active"
            lines.append(f"- {tool.name} [{state}]: {tool.description}")
        if len(hits) > limit:
            lines.append(f"… and {len(hits) - limit} more — refine the query or raise `limit`.")
        lines.append("To load a tool's schema, call select(name='<tool>').")
        return "\n".join(lines)

    def _activate(self, name: str) -> str:
        """Implement the `select` metatool: load a tool's schema and activate it."""
        if not self._registry:
            return "Error: no tools are registered, so nothing can be selected."
        tool = self._registry.get(name)
        if tool is None:
            similar = sorted(candidate for candidate in self._registry if name in candidate)[:5]
            hint = f" Did you mean one of {similar}?" if similar else ""
            available = ", ".join(sorted(self._registry)) or "none"
            return f"Error: no tool named {name!r}.{hint} Available tools: {available}."
        self._activated.add(name)
        try:
            schema = tool.tool_call_schema.model_json_schema()
        except AttributeError:
            schema = {}
        rendered = json.dumps(schema, indent=2, default=str)
        always = (
            "This tool is always visible; its schema is shown here for reference."
            if name not in self._deferred_names
            else "It will now be included in your available tools on every turn."
        )
        return f"Tool {name!r} is active. {always}\n\n{name} — {tool.description}\n\nSchema:\n{rendered}"
