"""On-premise / air-gapped deployment middleware.

Feature #23: Local model management and data flow policies to ensure
no data leaves the network in air-gapped environments.

## Tools

- `register_local_model`: Register a local model endpoint
- `set_data_policy`: Configure data flow policies
- `check_data_flow`: Check if an external request is allowed
- `air_gap_status`: View current air-gap configuration status
- `clear_air_gap`: Reset all air-gap configuration

## Usage

```python
from bog_agents.middleware.air_gapped import AirGappedMiddleware

middleware = AirGappedMiddleware()
```
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
class LocalModel:
    """A locally deployed model endpoint.

    Attributes:
        name: Model name.
        endpoint: Local endpoint URL.
        model_type: Type of model (llm, embedding, reranker).
        is_available: Whether the model is currently available.
    """

    name: str
    endpoint: str
    model_type: str = "llm"
    is_available: bool = True


@dataclass
class DataPolicy:
    """Data flow policy for air-gapped environments.

    Attributes:
        allow_external: Whether external network access is allowed.
        allowed_domains: List of domains that are explicitly allowed.
        blocked_patterns: List of patterns to block in outgoing data.
        audit_external: Whether to audit all external access attempts.
    """

    allow_external: bool = False
    allowed_domains: list[str] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    audit_external: bool = True


@dataclass
class AirGapStore:
    """In-memory store for air-gap configuration.

    Attributes:
        models: Registered local models keyed by name.
        policy: Active data flow policy.
        external_attempts: Log of external access attempts.
    """

    models: dict[str, LocalModel] = field(default_factory=dict)
    policy: DataPolicy = field(default_factory=DataPolicy)
    external_attempts: list[dict[str, str]] = field(default_factory=list)

    def register_model(
        self,
        name: str,
        endpoint: str,
        model_type: str = "llm",
        is_available: bool = True,
    ) -> LocalModel:
        """Register a local model endpoint.

        Args:
            name: Model name.
            endpoint: Local endpoint URL.
            model_type: Type of model.
            is_available: Whether the model is available.

        Returns:
            The registered local model.
        """
        model = LocalModel(
            name=name,
            endpoint=endpoint,
            model_type=model_type,
            is_available=is_available,
        )
        self.models[name] = model
        return model

    def set_policy(
        self,
        allow_external: bool | None = None,
        allowed_domains: list[str] | None = None,
        blocked_patterns: list[str] | None = None,
        audit_external: bool | None = None,
    ) -> DataPolicy:
        """Update the data flow policy.

        Args:
            allow_external: Whether to allow external access.
            allowed_domains: Allowed domain list.
            blocked_patterns: Blocked data patterns.
            audit_external: Whether to audit external attempts.

        Returns:
            Updated data policy.
        """
        if allow_external is not None:
            self.policy.allow_external = allow_external
        if allowed_domains is not None:
            self.policy.allowed_domains = allowed_domains
        if blocked_patterns is not None:
            self.policy.blocked_patterns = blocked_patterns
        if audit_external is not None:
            self.policy.audit_external = audit_external
        return self.policy

    def check_allowed(self, domain: str, data: str = "") -> tuple[bool, str]:
        """Check if an external request is allowed by policy.

        Args:
            domain: Target domain.
            data: Outgoing data to check against blocked patterns.

        Returns:
            Tuple of (is_allowed, reason).
        """
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())

        if not self.policy.allow_external:
            reason = "External access is disabled"
            if self.policy.audit_external:
                self.external_attempts.append({"domain": domain, "allowed": "false", "reason": reason, "timestamp": timestamp})
            return False, reason

        if self.policy.allowed_domains and domain not in self.policy.allowed_domains:
            reason = f"Domain '{domain}' is not in the allowed list"
            if self.policy.audit_external:
                self.external_attempts.append({"domain": domain, "allowed": "false", "reason": reason, "timestamp": timestamp})
            return False, reason

        for pattern in self.policy.blocked_patterns:
            if pattern.lower() in data.lower():
                reason = f"Data contains blocked pattern: '{pattern}'"
                if self.policy.audit_external:
                    self.external_attempts.append({"domain": domain, "allowed": "false", "reason": reason, "timestamp": timestamp})
                return False, reason

        if self.policy.audit_external:
            self.external_attempts.append({"domain": domain, "allowed": "true", "reason": "Passed all checks", "timestamp": timestamp})
        return True, "Allowed"

    def format_status(self) -> str:
        """Format the current air-gap status for display.

        Returns:
            Formatted status string.
        """
        lines = [
            "## Air-Gap Deployment Status",
            "",
            "### Data Policy",
            f"  External Access: {'ALLOWED' if self.policy.allow_external else 'BLOCKED'}",
            f"  Audit External:  {'ON' if self.policy.audit_external else 'OFF'}",
            f"  Allowed Domains: {', '.join(self.policy.allowed_domains) if self.policy.allowed_domains else 'None'}",
            f"  Blocked Patterns: {', '.join(self.policy.blocked_patterns) if self.policy.blocked_patterns else 'None'}",
            "",
            f"### Local Models ({len(self.models)})",
        ]
        if self.models:
            for model in self.models.values():
                status = "AVAILABLE" if model.is_available else "UNAVAILABLE"
                lines.append(f"  - {model.name} ({model.model_type}): {model.endpoint} [{status}]")
        else:
            lines.append("  No local models registered.")

        lines.append("")
        lines.append(f"### External Access Attempts: {len(self.external_attempts)}")
        for attempt in self.external_attempts[-5:]:
            lines.append(f"  - {attempt['domain']}: {attempt['allowed']} ({attempt['reason']}) at {attempt['timestamp']}")

        return "\n".join(lines)


AIR_GAPPED_SYSTEM_PROMPT = """## Air-Gapped Deployment Tools

You have access to tools for managing on-premise / air-gapped deployments.

**Available Tools:**
- `register_local_model`: Register local model endpoints
- `set_data_policy`: Configure data flow restrictions
- `check_data_flow`: Verify if external requests are allowed
- `air_gap_status`: View current deployment configuration
- `clear_air_gap`: Reset air-gap settings

**Guidelines:**
- Always check data flow policies before making external requests
- Prefer local models over external ones in air-gapped mode
- Audit logs track all external access attempts for compliance"""


class AirGappedState(TypedDict):
    """State for air-gapped middleware."""


class AirGappedMiddleware(AgentMiddleware[AirGappedState, ContextT, ResponseT]):
    """Middleware for on-premise and air-gapped deployment management.

    Provides tools for registering local models, configuring data flow
    policies, and auditing external access attempts.
    """

    state_schema = AirGappedState

    def __init__(self) -> None:
        self.store = AirGapStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build air-gapped deployment tools."""
        mw = self

        def register_local_model(
            runtime: ToolRuntime[None, AirGappedState],
            name: Annotated[str, "Model name"],
            endpoint: Annotated[str, "Local endpoint URL"],
            model_type: Annotated[str, "Model type: llm, embedding, reranker"] = "llm",
            is_available: Annotated[str, "Whether model is available (true/false)"] = "true",
        ) -> str:
            """Register a local model endpoint for air-gapped operation."""
            available = is_available.lower() in ("true", "yes", "1", "on")
            model = mw.store.register_model(
                name=name,
                endpoint=endpoint,
                model_type=model_type,
                is_available=available,
            )
            status = "AVAILABLE" if model.is_available else "UNAVAILABLE"
            return f"Registered local model '{model.name}' ({model.model_type}) at {model.endpoint} [{status}]. Total models: {len(mw.store.models)}"

        def set_data_policy(
            runtime: ToolRuntime[None, AirGappedState],
            allow_external: Annotated[str, "Allow external access (true/false)"] = "",
            allowed_domains: Annotated[str, "Comma-separated allowed domains"] = "",
            blocked_patterns: Annotated[str, "Comma-separated blocked data patterns"] = "",
            audit_external: Annotated[str, "Audit external attempts (true/false)"] = "",
        ) -> str:
            """Configure data flow policies for air-gapped operation."""
            ext = None
            if allow_external:
                ext = allow_external.lower() in ("true", "yes", "1", "on")
            domains = [d.strip() for d in allowed_domains.split(",") if d.strip()] if allowed_domains else None
            patterns = [p.strip() for p in blocked_patterns.split(",") if p.strip()] if blocked_patterns else None
            audit = None
            if audit_external:
                audit = audit_external.lower() in ("true", "yes", "1", "on")
            policy = mw.store.set_policy(
                allow_external=ext,
                allowed_domains=domains,
                blocked_patterns=patterns,
                audit_external=audit,
            )
            return f"Data policy updated: External={'ALLOWED' if policy.allow_external else 'BLOCKED'} | Domains={len(policy.allowed_domains)} | Patterns={len(policy.blocked_patterns)} | Audit={'ON' if policy.audit_external else 'OFF'}"

        def check_data_flow(
            runtime: ToolRuntime[None, AirGappedState],
            domain: Annotated[str, "Target domain to check"],
            data: Annotated[str, "Outgoing data to check against blocked patterns"] = "",
        ) -> str:
            """Check if an external request is allowed by the current data policy."""
            allowed, reason = mw.store.check_allowed(domain=domain, data=data)
            status = "ALLOWED" if allowed else "BLOCKED"
            return f"Data flow check: {domain} -> {status} ({reason})"

        def air_gap_status(
            runtime: ToolRuntime[None, AirGappedState],
        ) -> str:
            """View current air-gap configuration and deployment status."""
            return mw.store.format_status()

        def clear_air_gap(
            runtime: ToolRuntime[None, AirGappedState],
        ) -> str:
            """Reset all air-gap configuration."""
            models = len(mw.store.models)
            attempts = len(mw.store.external_attempts)
            mw.store = AirGapStore()
            return f"Cleared air-gap config: {models} models, {attempts} audit entries."

        return [
            StructuredTool.from_function(
                name="register_local_model", description="Register a local model endpoint for air-gapped operation.", func=register_local_model
            ),
            StructuredTool.from_function(
                name="set_data_policy", description="Configure data flow restrictions and audit settings.", func=set_data_policy
            ),
            StructuredTool.from_function(
                name="check_data_flow", description="Check if an external request is allowed by the data policy.", func=check_data_flow
            ),
            StructuredTool.from_function(
                name="air_gap_status", description="View current air-gap deployment configuration and audit log.", func=air_gap_status
            ),
            StructuredTool.from_function(name="clear_air_gap", description="Reset all air-gap configuration and audit data.", func=clear_air_gap),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject air-gapped deployment instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, AIR_GAPPED_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject air-gapped deployment instructions.

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


__all__ = ["AirGapStore", "AirGappedMiddleware", "DataPolicy", "LocalModel"]
