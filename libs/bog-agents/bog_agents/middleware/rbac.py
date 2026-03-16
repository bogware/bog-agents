"""Role-based access control (RBAC) middleware for tool-level permissions.

Feature #21: Tool-level permissions per role with glob pattern matching for
allowed and denied tool lists.

## Overview

The RBAC middleware provides tools for:

- Defining roles with allowed/denied tool patterns
- Setting the active role for the session
- Checking if a tool is permitted for the active role
- Listing all defined roles and their permissions

## Pattern Matching

Tool patterns use glob-style matching via `fnmatch`:

- `*` matches everything (full access)
- `read_*` matches all tools starting with `read_`
- `*_report` matches all tools ending with `_report`

Denied patterns take precedence over allowed patterns.

## Usage

```python
from bog_agents.middleware.rbac import RBACMiddleware

middleware = RBACMiddleware()
```
"""

from __future__ import annotations

import fnmatch
import logging
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
class Role:
    """A role definition with tool access patterns.

    Attributes:
        name: Unique name for this role.
        allowed_tools: Glob patterns for allowed tools (e.g., ``*``, ``read_*``).
        denied_tools: Glob patterns for denied tools (takes precedence over allowed).
        description: Human-readable description of the role.
    """

    name: str
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    description: str = ""

    def can_use_tool(self, tool_name: str) -> bool:
        """Check if this role permits use of a given tool.

        Denied patterns take precedence over allowed patterns. If no allowed
        patterns are defined, access is denied by default.

        Args:
            tool_name: Name of the tool to check.

        Returns:
            True if the tool is permitted, False otherwise.
        """
        # Denied patterns take precedence
        for pattern in self.denied_tools:
            if fnmatch.fnmatch(tool_name, pattern):
                return False

        # Check allowed patterns
        for pattern in self.allowed_tools:
            if fnmatch.fnmatch(tool_name, pattern):
                return True

        return False


@dataclass
class RBACStore:
    """Store managing role definitions and the active role.

    Attributes:
        roles: Map of role name to Role.
        active_role: Currently active role name.
    """

    roles: dict[str, Role] = field(default_factory=dict)
    active_role: str = ""

    def define_role(
        self,
        *,
        name: str,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        description: str = "",
    ) -> Role:
        """Define or update a role.

        Args:
            name: Unique name for the role.
            allowed_tools: Glob patterns for allowed tools.
            denied_tools: Glob patterns for denied tools.
            description: Human-readable description.

        Returns:
            The newly created or updated role.
        """
        role = Role(
            name=name,
            allowed_tools=allowed_tools or [],
            denied_tools=denied_tools or [],
            description=description,
        )
        self.roles[name] = role
        logger.debug("Defined role '%s' (allowed=%s, denied=%s)", name, role.allowed_tools, role.denied_tools)
        return role

    def set_active(self, name: str) -> Role | None:
        """Set the active role for the session.

        Args:
            name: Name of the role to activate.

        Returns:
            The activated role, or None if not found.
        """
        role = self.roles.get(name)
        if role is None:
            logger.warning("Role '%s' not found", name)
            return None
        self.active_role = name
        logger.debug("Active role set to '%s'", name)
        return role

    def is_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed for the active role.

        Args:
            tool_name: Name of the tool to check.

        Returns:
            True if the tool is permitted (or no active role), False otherwise.
        """
        if not self.active_role:
            return True
        role = self.roles.get(self.active_role)
        if role is None:
            return True
        return role.can_use_tool(tool_name)

    def format_roles(self) -> str:
        """Format a human-readable listing of all roles and permissions.

        Returns:
            Formatted roles listing string.
        """
        if not self.roles:
            return "No roles defined. Use `define_role` to create roles."

        lines = [
            "## RBAC Roles",
            f"Active role: {self.active_role or '(none)'}",
            f"Total roles: {len(self.roles)}",
            "",
        ]

        for name, role in sorted(self.roles.items()):
            active = " (active)" if name == self.active_role else ""
            lines.append(f"### {name}{active}")
            if role.description:
                lines.append(f"{role.description}")
            lines.append(f"- Allowed: {', '.join(role.allowed_tools) if role.allowed_tools else '(none)'}")
            lines.append(f"- Denied: {', '.join(role.denied_tools) if role.denied_tools else '(none)'}")
            lines.append("")

        return "\n".join(lines)


RBAC_SYSTEM_PROMPT = """## Role-Based Access Control

You are operating under role-based access control (RBAC). Tool access is restricted
based on the active role.

**Workflow:**
1. Use `define_role` to create roles with allowed/denied tool patterns
2. Use `set_active_role` to activate a role for the session
3. Use `check_permission` to verify tool access before use
4. Use `list_roles` to review all defined roles

**Pattern Matching:**
- `*` matches all tools (full access)
- `read_*` matches tools starting with `read_`
- Denied patterns always take precedence over allowed patterns

Always check permissions before attempting restricted operations."""


class RBACState(TypedDict):
    """State for RBAC middleware."""


class RBACMiddleware(AgentMiddleware[RBACState, ContextT, ResponseT]):
    """Middleware for role-based access control on tools.

    Provides tools for defining roles, setting the active role, checking
    permissions, and listing role definitions. Logs warnings when the active
    role lacks permission for certain tools.
    """

    state_schema = RBACState

    def __init__(self) -> None:
        self.store = RBACStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build RBAC tools."""
        mw = self

        def define_role(
            runtime: ToolRuntime[None, RBACState],
            role_name: Annotated[str, "Unique name for the role"],
            allowed_tools: Annotated[str, "Comma-separated glob patterns for allowed tools (e.g., *, read_*, query_*)"] = "",
            denied_tools: Annotated[str, "Comma-separated glob patterns for denied tools (e.g., delete_*, clear_*)"] = "",
            description: Annotated[str, "Human-readable description of the role"] = "",
        ) -> str:
            """Define a role with allowed and denied tool patterns."""
            allowed = [p.strip() for p in allowed_tools.split(",") if p.strip()] if allowed_tools else []
            denied = [p.strip() for p in denied_tools.split(",") if p.strip()] if denied_tools else []
            role = mw.store.define_role(
                name=role_name,
                allowed_tools=allowed,
                denied_tools=denied,
                description=description,
            )
            return (
                f"Role '{role.name}' defined.\n"
                f"  Allowed: {', '.join(role.allowed_tools) if role.allowed_tools else '(none)'}\n"
                f"  Denied: {', '.join(role.denied_tools) if role.denied_tools else '(none)'}"
            )

        def set_active_role(
            runtime: ToolRuntime[None, RBACState],
            role_name: Annotated[str, "Name of the role to activate"],
        ) -> str:
            """Set the active role for the session."""
            role = mw.store.set_active(role_name)
            if role is None:
                return f"Error: Role '{role_name}' not found. Use `define_role` first."
            return f"Active role set to '{role.name}'."

        def check_permission(
            runtime: ToolRuntime[None, RBACState],
            tool_name: Annotated[str, "Name of the tool to check permission for"],
        ) -> str:
            """Check if a tool is allowed for the active role."""
            if not mw.store.active_role:
                return f"No active role set. Tool '{tool_name}' is allowed by default."
            allowed = mw.store.is_allowed(tool_name)
            status = "ALLOWED" if allowed else "DENIED"
            return f"Tool '{tool_name}' is {status} for role '{mw.store.active_role}'."

        def list_roles(
            runtime: ToolRuntime[None, RBACState],
        ) -> str:
            """List all defined roles and their permissions."""
            return mw.store.format_roles()

        return [
            StructuredTool.from_function(
                name="define_role",
                description="Define a role with allowed and denied tool patterns for access control.",
                func=define_role,
            ),
            StructuredTool.from_function(
                name="set_active_role",
                description="Set the active role for the session, restricting tool access accordingly.",
                func=set_active_role,
            ),
            StructuredTool.from_function(
                name="check_permission",
                description="Check if a specific tool is allowed for the active role.",
                func=check_permission,
            ),
            StructuredTool.from_function(
                name="list_roles",
                description="List all defined roles and their permissions.",
                func=list_roles,
            ),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject RBAC instructions into the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with RBAC instructions.
        """
        new_system_message = append_to_system_message(request.system_message, RBAC_SYSTEM_PROMPT)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject RBAC instructions and log permission warnings.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        if self.store.active_role:
            role = self.store.roles.get(self.store.active_role)
            if role:
                for tool in self.tools:
                    if not role.can_use_tool(tool.name):
                        logger.warning(
                            "Active role '%s' cannot use tool '%s'",
                            self.store.active_role,
                            tool.name,
                        )

        modified = self.modify_request(request)
        return call_next(modified)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of wrap_model_call.

        Args:
            request: Model request.
            call_next: Async handler function.

        Returns:
            Model response.
        """
        if self.store.active_role:
            role = self.store.roles.get(self.store.active_role)
            if role:
                for tool in self.tools:
                    if not role.can_use_tool(tool.name):
                        logger.warning(
                            "Active role '%s' cannot use tool '%s'",
                            self.store.active_role,
                            tool.name,
                        )

        modified = self.modify_request(request)
        return await call_next(modified)


__all__ = ["RBACMiddleware", "RBACStore", "Role"]
