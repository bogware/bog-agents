"""Middleware for plan mode — read-only exploration without file mutations.

Feature #38: Plan mode — allows the agent to explore and plan without
making any file changes, then exit plan mode to execute.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

# Tools that are blocked in plan mode
_MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "multi_edit_file",
        "execute",
        "git_commit",
        "git_add",
        "git_stash",
    }
)

# Tools that are always allowed in plan mode
_READ_ONLY_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "read_many_files",
        "glob",
        "grep",
        "git_status",
        "git_diff",
        "git_log",
        "git_blame",
        "git_show",
        "repo_map",
        "detect_project",
        "show_cost",
        "show_context",
        "write_todos",
        "task",
    }
)

PLAN_MODE_PROMPT = """
## Plan Mode Active

You are currently in **plan mode**. In this mode:
- You can READ files, search code, explore the repository, and create plans
- You CANNOT write, edit, or execute any code
- Use this mode to thoroughly understand the task before making changes
- Create a detailed plan using write_todos to track your planned changes
- When ready to execute, tell the user to exit plan mode

Focus on:
1. Understanding the existing code structure
2. Identifying all files that need changes
3. Planning the specific changes for each file
4. Considering edge cases and test requirements
"""


class PlanModeState(TypedDict):
    """State for plan mode middleware."""


class PlanModeMiddleware(AgentMiddleware[PlanModeState, ContextT, ResponseT]):
    """Middleware that enforces read-only plan mode.

    When enabled, blocks all file-mutating and execution tools,
    allowing only read operations. This enables safe codebase
    exploration and planning without risk of unintended changes.

    Args:
        enabled: Whether plan mode is active.
    """

    state_schema = PlanModeState

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled
        self.tools = self._build_tools()

    @property
    def enabled(self) -> bool:
        """Whether plan mode is currently active."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Set plan mode state."""
        self._enabled = value

    def _build_tools(self) -> list[BaseTool]:
        """Build plan mode tools."""
        middleware = self

        def toggle_plan_mode(
            runtime: ToolRuntime[None, PlanModeState],
            enabled: bool = True,
        ) -> str:
            """Toggle plan mode on or off. In plan mode, only read operations are allowed."""
            middleware._enabled = enabled
            if enabled:
                return "Plan mode ENABLED. Only read operations are allowed. Create your plan, then disable plan mode to execute."
            return "Plan mode DISABLED. Full tool access restored. You can now execute your plan."

        return [
            StructuredTool.from_function(
                name="toggle_plan_mode",
                description="Toggle plan mode. When enabled, only read operations are allowed.",
                func=toggle_plan_mode,
            )
        ]

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject plan mode context and filter tools."""
        if self._enabled:
            request = append_to_system_message(request, PLAN_MODE_PROMPT)

            # Filter out mutating tools from the request
            if hasattr(request, "tools"):
                request.tools = [t for t in request.tools if getattr(t, "name", "") not in _MUTATING_TOOLS]

        return call_next(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        if self._enabled:
            request = append_to_system_message(request, PLAN_MODE_PROMPT)

            if hasattr(request, "tools"):
                request.tools = [t for t in request.tools if getattr(t, "name", "") not in _MUTATING_TOOLS]

        return await call_next(request)
