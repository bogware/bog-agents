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
from typing import Annotated, Any

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
    strict: bool = False
    """Deny-by-default posture. When True (set for an operator-pinned role), an
    empty active role or one naming an undefined role denies all tools rather
    than falling open. Unpinned/legacy stores stay permissive by default."""

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
            True if the tool is permitted, False otherwise. With no active role
            (or an undefined active role) a legacy store falls open (returns
            True); a `strict` store denies (returns False).
        """
        if not self.active_role:
            return not self.strict
        role = self.roles.get(self.active_role)
        if role is None:
            return not self.strict
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


RBAC_PINNED_SYSTEM_PROMPT = """## Role-Based Access Control

You are operating under an operator-pinned role. Tool access is restricted to
that role's allow-list and you CANNOT change the active role or redefine roles —
those are operator-only controls. Denied tools are removed from your tool set.

Use `check_permission` to see whether a tool is allowed and `list_roles` to
review the active policy."""


class RBACState(TypedDict):
    """State for RBAC middleware."""


class RBACMiddleware(AgentMiddleware[RBACState, ContextT, ResponseT]):
    """Middleware for role-based access control on tools.

    Two modes:

    - **Operator-pinned** (`active_role` provided at construction): the operator
      owns the policy. The model gets only the read-only `check_permission` /
      `list_roles` tools — it cannot call `define_role` / `set_active_role` to
      lift its own restrictions (MW-SAFE-2). The store is `strict`, so an empty
      or undefined pinned role denies all tools rather than falling open.
    - **Legacy/self-service** (no `active_role`): the historical behavior — all
      four tools are exposed and, until the model activates a role, access is
      unrestricted. This gives no boundary against an adversarial model and is
      intended only for cooperative, model-driven role experimentation.

    Args:
        roles: Role definitions to seed (operator-owned).
        active_role: The role to pin. When set, switches to operator-pinned mode.
    """

    state_schema = RBACState

    def __init__(
        self,
        *,
        roles: list[Role] | None = None,
        active_role: str = "",
    ) -> None:
        self.store = RBACStore()
        for role in roles or []:
            self.store.roles[role.name] = role
        self.store.active_role = active_role
        # Operator-pinned mode: policy is theirs to set; the model must not be
        # able to redefine roles or switch the active role, and deny-by-default.
        self._operator_pinned = bool(active_role)
        self.store.strict = self._operator_pinned
        if self._operator_pinned and active_role not in self.store.roles:
            logger.warning(
                "RBAC pinned to undefined role '%s'; denying all tools (deny-by-default).",
                active_role,
            )
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build RBAC tools.

        In operator-pinned mode the policy-mutation tools (`define_role`,
        `set_active_role`) are withheld from the model, leaving only the
        read-only `check_permission` / `list_roles` tools.
        """
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

        read_only_tools = [
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

        if self._operator_pinned:
            # The model cannot change an operator-pinned policy.
            return read_only_tools

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
            *read_only_tools,
        ]

    # RBAC's own admin tools are always permitted regardless of active role,
    # otherwise an over-restrictive role would lock the user out of role
    # management itself.
    _ADMIN_TOOLS: frozenset[str] = frozenset({"define_role", "set_active_role", "check_permission", "list_roles"})

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject RBAC instructions and filter disallowed tools.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with RBAC instructions and tools restricted to
            the active role's allow-list.
        """
        prompt = RBAC_PINNED_SYSTEM_PROMPT if self._operator_pinned else RBAC_SYSTEM_PROMPT
        new_system_message = append_to_system_message(request.system_message, prompt)

        if not self.store.active_role:
            # No active role: legacy stores fall open; a strict store denies all.
            if not self.store.strict:
                return request.override(system_message=new_system_message)
            role = None
        else:
            role = self.store.roles.get(self.store.active_role)

        if role is None and not self.store.strict:
            # Active role names an undefined role and we're not strict: fall open
            # (legacy behavior).
            return request.override(system_message=new_system_message)

        allowed_tools: list[BaseTool | dict[str, Any]] = []
        for tool in request.tools:
            tool_name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            if not tool_name:
                allowed_tools.append(tool)
                continue
            # In strict mode with an undefined role, only the (read-only) admin
            # tools survive — everything else is denied by default.
            permitted = tool_name in self._ADMIN_TOOLS or (role is not None and role.can_use_tool(tool_name))
            if permitted:
                allowed_tools.append(tool)
            else:
                logger.warning(
                    "RBAC: stripping tool '%s' from request — denied by role '%s'",
                    tool_name,
                    self.store.active_role or "(none)",
                )

        return request.override(system_message=new_system_message, tools=allowed_tools)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject RBAC instructions and enforce tool access.

        Args:
            request: Model request.
            call_next: Handler function.

        Returns:
            Model response.
        """
        return call_next(self.modify_request(request))

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
        return await call_next(self.modify_request(request))


__all__ = ["RBACMiddleware", "RBACStore", "Role"]
