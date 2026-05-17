"""SSO / OAuth integration middleware.

⚠ **NOTSECURE — DEMO STUB, NOT REAL AUTHENTICATION.**

This middleware exposes a SAML/OIDC-shaped API but does NOT verify any
identity assertion. ``authenticate(user_id, provider, roles, …)`` simply
records whatever the caller (typically the LLM itself) claims. There is
no signature check, no provider round-trip, no token validation, no IdP
metadata verification.

Use this only for:

* UI prototyping where you want auth-shaped fields in the audit log but
  don't yet have a real IdP integration.
* Local development against a fake provider.

DO NOT use this in any flow whose access decisions matter — the LLM can
self-authenticate as any user with any roles, simply by calling the
tool. Fixes the misleading framing flagged in REVIEW.md P1-7. The plan
is to either:

1. Wrap a real OIDC library (``authlib``, ``oauthlib``) for the
   authenticate path, or
2. Rename this module to ``mock_auth.py`` and ship a real SSO middleware
   in a separate package.

Until one of those lands, the middleware is disabled by default in
``create_agent`` and emits a runtime warning on first instantiation.

## Tools

- `register_sso_provider`: Register a SAML/OIDC provider (no verification)
- `authenticate`: Create an authenticated session (no verification)
- `whoami`: Show current session information
- `auth_status`: View all providers and sessions
- `clear_auth`: Clear all authentication data

## Usage

```python
from bog_agents.middleware.sso_auth import SSOAuthMiddleware

middleware = SSOAuthMiddleware()  # logs a NOTSECURE warning
```
"""

from __future__ import annotations

import hashlib
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
class SSOProvider:
    """An SSO identity provider configuration.

    Attributes:
        name: Provider name.
        protocol: Authentication protocol (saml, oidc).
        issuer_url: Issuer URL for the provider.
        client_id: Client identifier.
        is_configured: Whether the provider is fully configured.
    """

    name: str
    protocol: str
    issuer_url: str
    client_id: str
    is_configured: bool = True


@dataclass
class AuthSession:
    """An authenticated user session.

    Attributes:
        session_id: Unique session identifier.
        user_id: Authenticated user identifier.
        provider: Name of the SSO provider used.
        roles: List of assigned roles.
        authenticated_at: ISO timestamp of authentication.
        expires_at: ISO timestamp of session expiry.
    """

    session_id: str
    user_id: str
    provider: str
    roles: list[str] = field(default_factory=list)
    authenticated_at: str = ""
    expires_at: str = ""


@dataclass
class AuthStore:
    """In-memory store for SSO providers and sessions.

    Attributes:
        providers: Registered SSO providers keyed by name.
        sessions: Active sessions keyed by session_id.
        active_session_id: Currently active session ID.
    """

    providers: dict[str, SSOProvider] = field(default_factory=dict)
    sessions: dict[str, AuthSession] = field(default_factory=dict)
    active_session_id: str = ""

    def register_provider(
        self,
        name: str,
        protocol: str,
        issuer_url: str,
        client_id: str,
    ) -> SSOProvider:
        """Register an SSO identity provider.

        Args:
            name: Provider name.
            protocol: Authentication protocol.
            issuer_url: Issuer URL.
            client_id: Client identifier.

        Returns:
            The registered provider.
        """
        provider = SSOProvider(
            name=name,
            protocol=protocol,
            issuer_url=issuer_url,
            client_id=client_id,
            is_configured=True,
        )
        self.providers[name] = provider
        return provider

    def create_session(
        self,
        user_id: str,
        provider: str,
        roles: list[str] | None = None,
        duration_hours: int = 8,
    ) -> AuthSession:
        """Create an authenticated session.

        Args:
            user_id: User identifier.
            provider: SSO provider name.
            roles: List of roles to assign.
            duration_hours: Session duration in hours.

        Returns:
            The created session.
        """
        now = time.time()
        session_id = hashlib.sha256(f"{user_id}-{now}".encode()).hexdigest()[:16]
        session = AuthSession(
            session_id=session_id,
            user_id=user_id,
            provider=provider,
            roles=roles or [],
            authenticated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            expires_at=time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.gmtime(now + duration_hours * 3600),
            ),
        )
        self.sessions[session_id] = session
        self.active_session_id = session_id
        return session

    def get_active_session(self) -> AuthSession | None:
        """Get the currently active session.

        Returns:
            The active session, or None if no active session.
        """
        if self.active_session_id and self.active_session_id in self.sessions:
            return self.sessions[self.active_session_id]
        return None

    def format_status(self) -> str:
        """Format authentication status for display.

        Returns:
            Formatted status string.
        """
        lines = [
            "## SSO / Authentication Status",
            "",
            f"### Providers ({len(self.providers)})",
        ]
        if self.providers:
            for provider in self.providers.values():
                status = "CONFIGURED" if provider.is_configured else "PENDING"
                lines.append(f"  - {provider.name} ({provider.protocol}): {provider.issuer_url} [{status}]")
        else:
            lines.append("  No providers registered.")

        lines.append("")
        lines.append(f"### Sessions ({len(self.sessions)})")
        active = self.get_active_session()
        if self.sessions:
            for session in self.sessions.values():
                marker = " **[ACTIVE]**" if session.session_id == self.active_session_id else ""
                lines.append(f"  - {session.session_id}: {session.user_id} via {session.provider}{marker}")
                lines.append(f"    Roles: {', '.join(session.roles) if session.roles else 'None'}")
                lines.append(f"    Auth: {session.authenticated_at} | Expires: {session.expires_at}")
        else:
            lines.append("  No active sessions.")

        return "\n".join(lines)


SSO_AUTH_SYSTEM_PROMPT = """## SSO / OAuth Integration Tools

You have access to SSO authentication and session management tools.

**Available Tools:**
- `register_sso_provider`: Register SAML/OIDC identity providers
- `authenticate`: Create an authenticated session with roles
- `whoami`: Check current session and user information
- `auth_status`: View all providers and sessions
- `clear_auth`: Reset all authentication data

**Guidelines:**
- Always verify authentication before accessing sensitive data
- Use role-based access to enforce permissions
- Sessions have configurable expiry times for security"""


class SSOAuthState(TypedDict):
    """State for SSO auth middleware."""


class SSOAuthMiddleware(AgentMiddleware[SSOAuthState, ContextT, ResponseT]):
    """Middleware for SSO and OAuth authentication management.

    Provides tools for registering identity providers, creating authenticated
    sessions, and managing role-based access control.
    """

    state_schema = SSOAuthState

    _NOTSECURE_WARNING_FIRED = False

    def __init__(self) -> None:
        # P1-7: surface the stub-not-real-auth status at instantiation
        # time so a developer who casually drops this middleware into a
        # stack can't miss the warning. Logged once per process to avoid
        # log spam.
        if not SSOAuthMiddleware._NOTSECURE_WARNING_FIRED:
            SSOAuthMiddleware._NOTSECURE_WARNING_FIRED = True
            import warnings

            warnings.warn(
                "SSOAuthMiddleware is a DEMO STUB — it records identity "
                "assertions but does not verify them. Do not use for "
                "access decisions. See module docstring (REVIEW.md P1-7).",
                UserWarning,
                stacklevel=2,
            )
        self.store = AuthStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build SSO authentication tools."""
        mw = self

        def register_sso_provider(
            runtime: ToolRuntime[None, SSOAuthState],
            name: Annotated[str, "Provider name"],
            protocol: Annotated[str, "Protocol: saml or oidc"],
            issuer_url: Annotated[str, "Issuer URL"],
            client_id: Annotated[str, "Client identifier"],
        ) -> str:
            """Register a SAML/OIDC identity provider."""
            provider = mw.store.register_provider(
                name=name,
                protocol=protocol,
                issuer_url=issuer_url,
                client_id=client_id,
            )
            return f"Registered SSO provider '{provider.name}' ({provider.protocol}) at {provider.issuer_url}. Total providers: {len(mw.store.providers)}"

        def authenticate(
            runtime: ToolRuntime[None, SSOAuthState],
            user_id: Annotated[str, "User identifier"],
            provider: Annotated[str, "SSO provider name"],
            roles: Annotated[str, "Comma-separated list of roles"] = "",
            duration_hours: Annotated[int, "Session duration in hours"] = 8,
        ) -> str:
            """Create an authenticated session for a user."""
            if provider not in mw.store.providers:
                return f"Provider '{provider}' not registered. Use `register_sso_provider` first."
            role_list = [r.strip() for r in roles.split(",") if r.strip()] if roles else []
            session = mw.store.create_session(
                user_id=user_id,
                provider=provider,
                roles=role_list,
                duration_hours=duration_hours,
            )
            return f"Authenticated {session.user_id} via {session.provider} (session: {session.session_id}). Roles: {', '.join(session.roles) if session.roles else 'None'}. Expires: {session.expires_at}"

        def whoami(
            runtime: ToolRuntime[None, SSOAuthState],
        ) -> str:
            """Show current session information."""
            session = mw.store.get_active_session()
            if not session:
                return "No active session. Use `authenticate` to log in."
            lines = [
                "## Current Session",
                f"  User:     {session.user_id}",
                f"  Provider: {session.provider}",
                f"  Roles:    {', '.join(session.roles) if session.roles else 'None'}",
                f"  Session:  {session.session_id}",
                f"  Auth:     {session.authenticated_at}",
                f"  Expires:  {session.expires_at}",
            ]
            return "\n".join(lines)

        def auth_status(
            runtime: ToolRuntime[None, SSOAuthState],
        ) -> str:
            """View all providers and sessions."""
            return mw.store.format_status()

        def clear_auth(
            runtime: ToolRuntime[None, SSOAuthState],
        ) -> str:
            """Clear all authentication data."""
            providers = len(mw.store.providers)
            sessions = len(mw.store.sessions)
            mw.store = AuthStore()
            return f"Cleared auth data: {providers} providers, {sessions} sessions."

        return [
            StructuredTool.from_function(
                name="register_sso_provider", description="Register a SAML/OIDC identity provider for SSO.", func=register_sso_provider
            ),
            StructuredTool.from_function(
                name="authenticate", description="Create an authenticated session with roles via an SSO provider.", func=authenticate
            ),
            StructuredTool.from_function(name="whoami", description="Show current authenticated session and user information.", func=whoami),
            StructuredTool.from_function(name="auth_status", description="View all registered providers and active sessions.", func=auth_status),
            StructuredTool.from_function(name="clear_auth", description="Clear all SSO providers and authentication sessions.", func=clear_auth),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject SSO authentication instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, SSO_AUTH_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject SSO authentication instructions.

        Args:
            request: Model request.
            call_next: Handler.

        Returns:
            Model response.
        """
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version.

        Args:
            request: Model request.
            call_next: Async handler.

        Returns:
            Model response.
        """
        return await call_next(self.modify_request(request))


__all__ = ["AuthSession", "AuthStore", "SSOAuthMiddleware", "SSOProvider"]
