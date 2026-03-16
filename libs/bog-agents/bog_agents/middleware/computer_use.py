"""Computer Use middleware for desktop automation.

Control desktop applications like Bloomberg, Excel, CRM, and email
through programmatic actions for financial advisory workflows.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

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

APPLICATIONS = ["bloomberg", "excel", "crm", "email", "browser", "custom"]
ACTION_TYPES = ["click", "type", "read", "navigate", "extract", "execute_macro"]


@dataclass
class DesktopAction:
    """A single desktop automation action."""

    action_id: str
    application: str
    action_type: str
    parameters: dict[str, str] = field(default_factory=dict)
    result: str = ""
    timestamp: str = ""


@dataclass
class DesktopSession:
    """Tracks a desktop automation session with connected apps and actions."""

    session_id: str
    actions: list[DesktopAction] = field(default_factory=list)
    connected_apps: list[str] = field(default_factory=list)
    _next_id: int = 1


SYSTEM_PROMPT = """You have access to desktop automation tools. You can:
- Connect to applications: bloomberg, excel, crm, email, browser, custom
- Perform actions: click, type, read, navigate, extract, execute_macro
- List connected applications and their status
- Review action history for audit trails
Use these tools to automate desktop workflows for financial advisory tasks."""


class ComputerUseState(TypedDict):
    """State for the computer use middleware."""


class ComputerUseMiddleware(AgentMiddleware[ComputerUseState, ContextT, ResponseT]):
    """Middleware for desktop application automation."""

    state_schema = ComputerUseState

    def __init__(self) -> None:
        self.session = DesktopSession(session_id="default")
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        mw = self

        def connect_application(
            runtime: ToolRuntime[None, ComputerUseState],
            application: Annotated[str, "Application to connect: bloomberg, excel, crm, email, browser, custom"],
        ) -> str:
            """Connect to a desktop application for automation."""
            if application not in APPLICATIONS:
                return f"Error: Invalid application '{application}'. Must be one of: {', '.join(APPLICATIONS)}"
            if application in mw.session.connected_apps:
                return f"Application '{application}' is already connected."
            mw.session.connected_apps.append(application)
            logger.info("Connected to application: %s", application)
            return f"Connected to '{application}'"

        def desktop_action(
            runtime: ToolRuntime[None, ComputerUseState],
            application: Annotated[str, "Target application for the action"],
            action_type: Annotated[str, "Type of action: click, type, read, navigate, extract, execute_macro"],
            target: Annotated[str, "Target element or location for the action"],
            value: Annotated[str, "Value to use for the action (e.g., text to type)"],
        ) -> str:
            """Perform a desktop automation action on a connected application."""
            if application not in mw.session.connected_apps:
                return f"Error: Application '{application}' is not connected. Connect it first."
            if action_type not in ACTION_TYPES:
                return f"Error: Invalid action type '{action_type}'. Must be one of: {', '.join(ACTION_TYPES)}"
            aid = f"dact-{mw.session._next_id}"
            mw.session._next_id += 1
            action = DesktopAction(
                action_id=aid,
                application=application,
                action_type=action_type,
                parameters={"target": target, "value": value},
                result=f"Performed {action_type} on '{target}' in {application}",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.session.actions.append(action)
            logger.info("Desktop action %s: %s on %s", aid, action_type, application)
            return f"Action {aid}: {action_type} on '{target}' in {application} completed"

        def list_connected_apps(
            runtime: ToolRuntime[None, ComputerUseState],
        ) -> str:
            """List all currently connected desktop applications."""
            if not mw.session.connected_apps:
                return "No applications connected."
            lines = [f"Connected applications ({len(mw.session.connected_apps)}):"]
            for app in mw.session.connected_apps:
                lines.append(f"  - {app}")
            return "\n".join(lines)

        def action_history(
            runtime: ToolRuntime[None, ComputerUseState],
        ) -> str:
            """Return the history of desktop automation actions."""
            if not mw.session.actions:
                return "No actions recorded."
            lines = [f"Action history ({len(mw.session.actions)} actions):"]
            for action in mw.session.actions:
                lines.append(f"  - {action.action_id}: [{action.application}] {action.action_type} at {action.timestamp}")
            return "\n".join(lines)

        def clear_desktop(
            runtime: ToolRuntime[None, ComputerUseState],
        ) -> str:
            """Clear the desktop session, disconnecting all apps and clearing history."""
            app_count = len(mw.session.connected_apps)
            action_count = len(mw.session.actions)
            mw.session = DesktopSession(session_id="default")
            logger.info("Cleared desktop session (%d apps, %d actions)", app_count, action_count)
            return f"Cleared desktop session ({app_count} app(s), {action_count} action(s))."

        return [
            StructuredTool.from_function(connect_application, name="connect_application"),
            StructuredTool.from_function(desktop_action, name="desktop_action"),
            StructuredTool.from_function(list_connected_apps, name="list_connected_apps"),
            StructuredTool.from_function(action_history, name="action_history"),
            StructuredTool.from_function(clear_desktop, name="clear_desktop"),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the computer use system prompt to the request."""
        return request.override(system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Synchronously wrap the model call with desktop automation context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Asynchronously wrap the model call with desktop automation context."""
        return await call_next(self.modify_request(request))


__all__ = ["ComputerUseMiddleware", "DesktopAction", "DesktopSession"]
