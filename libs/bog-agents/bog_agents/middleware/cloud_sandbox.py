"""Cloud Sandbox middleware for isolated environments.

Provision and manage isolated cloud environments preloaded with firm data
for safe experimentation and testing of financial workflows.
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

ENVIRONMENTS = ["development", "staging", "production", "testing"]


@dataclass
class SandboxConfig:
    """Configuration for a cloud sandbox environment."""

    sandbox_id: str
    name: str
    environment: str
    preloaded_data: list[str] = field(default_factory=list)
    status: str = "created"
    created_at: str = ""


@dataclass
class SandboxStore:
    """In-memory store for cloud sandbox environments."""

    sandboxes: dict[str, SandboxConfig] = field(default_factory=dict)
    active_sandbox: str = ""
    _next_id: int = 1


SYSTEM_PROMPT = """You have access to cloud sandbox management tools. You can:
- Create isolated sandbox environments (development, staging, production, testing)
- Activate a sandbox as the current working environment
- List all sandboxes and their configurations
- View detailed status of any sandbox
Use these tools to manage isolated environments for financial data analysis and testing."""


class CloudSandboxState(TypedDict):
    """State for the cloud sandbox middleware."""


class CloudSandboxMiddleware(AgentMiddleware[CloudSandboxState, ContextT, ResponseT]):
    """Middleware for managing isolated cloud sandbox environments."""

    state_schema = CloudSandboxState

    def __init__(self) -> None:
        self.store = SandboxStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        mw = self

        def create_sandbox(
            runtime: ToolRuntime[None, CloudSandboxState],
            name: Annotated[str, "Name of the sandbox environment"],
            environment: Annotated[str, "Environment type: development, staging, production, testing"],
            preloaded_data: Annotated[str, "Comma-separated list of datasets to preload"],
        ) -> str:
            """Create a new isolated cloud sandbox environment."""
            if environment not in ENVIRONMENTS:
                return f"Error: Invalid environment '{environment}'. Must be one of: {', '.join(ENVIRONMENTS)}"
            sid = f"sandbox-{mw.store._next_id}"
            mw.store._next_id += 1
            data_list = [d.strip() for d in preloaded_data.split(",") if d.strip()]
            sandbox = SandboxConfig(
                sandbox_id=sid,
                name=name,
                environment=environment,
                preloaded_data=data_list,
                status="running",
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            )
            mw.store.sandboxes[sid] = sandbox
            logger.info("Created sandbox %s: %s (%s)", sid, name, environment)
            return f"Created sandbox '{name}' (ID: {sid}, env: {environment}) with {len(data_list)} preloaded dataset(s)"

        def activate_sandbox(
            runtime: ToolRuntime[None, CloudSandboxState],
            sandbox_id: Annotated[str, "ID of the sandbox to activate"],
        ) -> str:
            """Activate a sandbox as the current working environment."""
            sandbox = mw.store.sandboxes.get(sandbox_id)
            if not sandbox:
                return f"Error: Sandbox '{sandbox_id}' not found."
            if sandbox.status != "running":
                return f"Error: Sandbox '{sandbox_id}' is not running (status: {sandbox.status})."
            mw.store.active_sandbox = sandbox_id
            logger.info("Activated sandbox %s", sandbox_id)
            return f"Activated sandbox '{sandbox.name}' ({sandbox_id})"

        def list_sandboxes(
            runtime: ToolRuntime[None, CloudSandboxState],
        ) -> str:
            """List all cloud sandbox environments."""
            if not mw.store.sandboxes:
                return "No sandboxes configured."
            lines = [f"Sandboxes ({len(mw.store.sandboxes)}):"]
            for sid, sb in mw.store.sandboxes.items():
                active = " [ACTIVE]" if sid == mw.store.active_sandbox else ""
                lines.append(f"  - {sid}: {sb.name} ({sb.environment}) [{sb.status}]{active}")
            return "\n".join(lines)

        def sandbox_status(
            runtime: ToolRuntime[None, CloudSandboxState],
            sandbox_id: Annotated[str, "ID of the sandbox to check"],
        ) -> str:
            """Get detailed status of a specific sandbox."""
            sandbox = mw.store.sandboxes.get(sandbox_id)
            if not sandbox:
                return f"Error: Sandbox '{sandbox_id}' not found."
            is_active = sandbox_id == mw.store.active_sandbox
            lines = [
                f"Sandbox: {sandbox.name} ({sandbox.sandbox_id})",
                f"  Environment: {sandbox.environment}",
                f"  Status: {sandbox.status}",
                f"  Active: {'yes' if is_active else 'no'}",
                f"  Created at: {sandbox.created_at}",
                f"  Preloaded data ({len(sandbox.preloaded_data)}):",
            ]
            for data in sandbox.preloaded_data:
                lines.append(f"    - {data}")
            return "\n".join(lines)

        def clear_sandboxes(
            runtime: ToolRuntime[None, CloudSandboxState],
        ) -> str:
            """Clear all sandbox environments."""
            count = len(mw.store.sandboxes)
            mw.store = SandboxStore()
            logger.info("Cleared %d sandboxes", count)
            return f"Cleared {count} sandbox(es)."

        return [
            StructuredTool.from_function(create_sandbox, name="create_sandbox"),
            StructuredTool.from_function(activate_sandbox, name="activate_sandbox"),
            StructuredTool.from_function(list_sandboxes, name="list_sandboxes"),
            StructuredTool.from_function(sandbox_status, name="sandbox_status"),
            StructuredTool.from_function(clear_sandboxes, name="clear_sandboxes"),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the cloud sandbox system prompt to the request."""
        return request.override(system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Synchronously wrap the model call with sandbox context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Asynchronously wrap the model call with sandbox context."""
        return await call_next(self.modify_request(request))


__all__ = ["CloudSandboxMiddleware", "SandboxConfig", "SandboxStore"]
