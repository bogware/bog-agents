"""Browser Agent middleware for financial advisors.

Navigate web pages, fill forms, and extract DOM data for financial research
and data gathering workflows.
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


@dataclass
class BrowserAction:
    """A single browser action taken during a session."""

    action_id: str
    url: str
    action_type: str
    selector: str
    value: str
    result: str
    timestamp: str


@dataclass
class BrowserSession:
    """Tracks a browser session with its actions and navigation history."""

    session_id: str
    actions: list[BrowserAction] = field(default_factory=list)
    current_url: str = ""
    history: list[str] = field(default_factory=list)


SYSTEM_PROMPT = """You have access to a browser agent for financial research. You can:
- Navigate to URLs to gather financial data
- Extract content from web pages (DOM elements, tables, text)
- Fill forms for financial queries and searches
- Review browser history for audit trails
Use these tools to help the financial advisor gather data from web sources."""


class BrowserAgentFAState(TypedDict):
    """State for the browser agent middleware."""


class BrowserAgentFAMiddleware(AgentMiddleware[BrowserAgentFAState, ContextT, ResponseT]):
    """Middleware providing browser automation for financial advisors."""

    state_schema = BrowserAgentFAState

    def __init__(self) -> None:
        self.session = BrowserSession(session_id="default")
        self._next_action_id = 1
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        mw = self

        def navigate_to(
            runtime: ToolRuntime[None, BrowserAgentFAState],
            url: Annotated[str, "The URL to navigate to"],
        ) -> str:
            """Navigate the browser to a specified URL."""
            mw.session.current_url = url
            mw.session.history.append(url)
            action = BrowserAction(
                action_id=f"act-{mw._next_action_id}",
                url=url,
                action_type="navigate",
                selector="",
                value="",
                result=f"Navigated to {url}",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw._next_action_id += 1
            mw.session.actions.append(action)
            logger.info("Navigated to %s", url)
            return f"Successfully navigated to {url}"

        def extract_content(
            runtime: ToolRuntime[None, BrowserAgentFAState],
            selector: Annotated[str, "CSS selector to extract content from"],
        ) -> str:
            """Extract content from the current page using a CSS selector."""
            if not mw.session.current_url:
                return "Error: No page loaded. Navigate to a URL first."
            action = BrowserAction(
                action_id=f"act-{mw._next_action_id}",
                url=mw.session.current_url,
                action_type="extract",
                selector=selector,
                value="",
                result=f"Extracted content from '{selector}' on {mw.session.current_url}",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw._next_action_id += 1
            mw.session.actions.append(action)
            logger.info("Extracted content using selector '%s'", selector)
            return f"Extracted content from '{selector}' on {mw.session.current_url}"

        def fill_form(
            runtime: ToolRuntime[None, BrowserAgentFAState],
            selector: Annotated[str, "CSS selector for the form field"],
            value: Annotated[str, "Value to fill in the form field"],
        ) -> str:
            """Fill a form field on the current page."""
            if not mw.session.current_url:
                return "Error: No page loaded. Navigate to a URL first."
            action = BrowserAction(
                action_id=f"act-{mw._next_action_id}",
                url=mw.session.current_url,
                action_type="fill",
                selector=selector,
                value=value,
                result=f"Filled '{selector}' with value on {mw.session.current_url}",
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw._next_action_id += 1
            mw.session.actions.append(action)
            logger.info("Filled form field '%s'", selector)
            return f"Filled '{selector}' with provided value"

        def browser_history(
            runtime: ToolRuntime[None, BrowserAgentFAState],
        ) -> str:
            """Return the browser navigation history for the current session."""
            if not mw.session.history:
                return "No browser history yet."
            lines = [f"Browser history ({len(mw.session.history)} entries):"]
            for i, url in enumerate(mw.session.history, 1):
                lines.append(f"  {i}. {url}")
            lines.append(f"Current URL: {mw.session.current_url}")
            return "\n".join(lines)

        def clear_browser(
            runtime: ToolRuntime[None, BrowserAgentFAState],
        ) -> str:
            """Clear the browser session, history, and all actions."""
            mw.session = BrowserSession(session_id="default")
            mw._next_action_id = 1
            logger.info("Browser session cleared")
            return "Browser session cleared."

        return [
            StructuredTool.from_function(navigate_to, name="navigate_to"),
            StructuredTool.from_function(extract_content, name="extract_content"),
            StructuredTool.from_function(fill_form, name="fill_form"),
            StructuredTool.from_function(browser_history, name="browser_history"),
            StructuredTool.from_function(clear_browser, name="clear_browser"),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the browser agent system prompt to the request."""
        return request.override(system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Synchronously wrap the model call with browser agent context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Asynchronously wrap the model call with browser agent context."""
        return await call_next(self.modify_request(request))


__all__ = ["BrowserAction", "BrowserAgentFAMiddleware", "BrowserSession"]
