"""Firm-wide deployment mode middleware.
Feature #22: Central configuration, usage analytics, and user management
for enterprise-wide deployment of the financial advisor agent.

## Tools

- `set_firm_config`: Configure firm-wide settings
- `record_usage`: Record a usage event for analytics
- `usage_analytics`: View usage analytics and statistics
- `list_active_users`: List currently active users
- `clear_firm_data`: Clear all firm configuration and usage data

## Usage

```python
from bog_agents.middleware.firm_deployment import FirmDeploymentMiddleware

middleware = FirmDeploymentMiddleware()
```

⚠ **STUB — NOT FOR PRODUCTION USE.**

This middleware is a scaffold that demonstrates the shape of a real
implementation. Its tools accept calls and return placeholder structures
so an agent can be wired against the surface, but the underlying logic
is not implemented — for example, ``fetch_quote`` returns ``price=0.0``
with a note instructing the caller to populate real data. Models that
call these tools will receive plausible-looking but **incorrect**
results.

This module ships at "Development Status :: 4 - Beta" deliberately;
see REVIEW.md P0-A for the broader plan (extract to a separate
``bog-agents-finance``-style package once the implementations are real,
or remove from the headline middleware list if they will not be).
Do not enable in any flow whose output is consumed by a downstream
system, customer-facing surface, or compliance-relevant artifact.
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
class FirmConfig:
    """Firm-wide configuration settings.

    Attributes:
        firm_name: Name of the firm.
        allowed_models: List of allowed model identifiers.
        default_model: Default model to use.
        compliance_mode: Whether compliance mode is enabled.
        max_tokens_per_session: Maximum tokens allowed per session.
        custom_settings: Additional custom key-value settings.
    """

    firm_name: str = ""
    allowed_models: list[str] = field(default_factory=list)
    default_model: str = ""
    compliance_mode: bool = False
    max_tokens_per_session: int = 100000
    custom_settings: dict[str, str] = field(default_factory=dict)


@dataclass
class UsageRecord:
    """A single usage event record.

    Attributes:
        user_id: Identifier of the user.
        action: Action performed.
        timestamp: ISO timestamp of the event.
        tokens_used: Number of tokens consumed.
    """

    user_id: str
    action: str
    timestamp: str
    tokens_used: int = 0


@dataclass
class FirmStore:
    """In-memory store for firm configuration and usage data.

    Attributes:
        config: Firm-wide configuration.
        usage_records: List of usage records.
        active_users: Set of currently active user IDs.
    """

    config: FirmConfig = field(default_factory=FirmConfig)
    usage_records: list[UsageRecord] = field(default_factory=list)
    active_users: set[str] = field(default_factory=set)

    def set_config(
        self,
        firm_name: str = "",
        allowed_models: list[str] | None = None,
        default_model: str = "",
        compliance_mode: bool | None = None,
        max_tokens_per_session: int | None = None,
    ) -> FirmConfig:
        """Update firm configuration.

        Args:
            firm_name: Name of the firm.
            allowed_models: Allowed model identifiers.
            default_model: Default model.
            compliance_mode: Whether to enable compliance mode.
            max_tokens_per_session: Max tokens per session.

        Returns:
            Updated firm configuration.
        """
        if firm_name:
            self.config.firm_name = firm_name
        if allowed_models is not None:
            self.config.allowed_models = allowed_models
        if default_model:
            self.config.default_model = default_model
        if compliance_mode is not None:
            self.config.compliance_mode = compliance_mode
        if max_tokens_per_session is not None:
            self.config.max_tokens_per_session = max_tokens_per_session
        return self.config

    def record_usage(self, user_id: str, action: str, tokens_used: int = 0) -> UsageRecord:
        """Record a usage event.

        Args:
            user_id: User identifier.
            action: Action performed.
            tokens_used: Tokens consumed.

        Returns:
            The recorded usage event.
        """
        record = UsageRecord(
            user_id=user_id,
            action=action,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            tokens_used=tokens_used,
        )
        self.usage_records.append(record)
        self.active_users.add(user_id)
        return record

    def format_analytics(self) -> str:
        """Format usage analytics for display.

        Returns:
            Formatted analytics string.
        """
        if not self.usage_records:
            return "No usage data recorded."

        total_tokens = sum(r.tokens_used for r in self.usage_records)
        users = {r.user_id for r in self.usage_records}
        actions: dict[str, int] = {}
        for record in self.usage_records:
            actions[record.action] = actions.get(record.action, 0) + 1

        lines = [
            f"## Firm Usage Analytics: {self.config.firm_name or 'Unconfigured'}",
            f"Total Events: {len(self.usage_records)} | Unique Users: {len(users)} | Total Tokens: {total_tokens:,}",
            "",
            "### Actions Breakdown",
        ]
        for action, count in sorted(actions.items(), key=lambda x: -x[1]):
            lines.append(f"  {action:<30s} {count:>6d}")

        lines.append("")
        lines.append("### Configuration")
        lines.append(f"  Compliance Mode: {'ON' if self.config.compliance_mode else 'OFF'}")
        lines.append(f"  Max Tokens/Session: {self.config.max_tokens_per_session:,}")
        lines.append(f"  Default Model: {self.config.default_model or 'Not set'}")
        lines.append(f"  Allowed Models: {', '.join(self.config.allowed_models) if self.config.allowed_models else 'All'}")

        return "\n".join(lines)


FIRM_DEPLOYMENT_SYSTEM_PROMPT = """## Firm-Wide Deployment Tools

You have access to firm deployment management tools for enterprise configuration
and usage analytics.

**Available Tools:**
- `set_firm_config`: Configure firm-wide settings (models, compliance, tokens)
- `record_usage`: Log usage events for analytics tracking
- `usage_analytics`: View aggregated usage statistics
- `list_active_users`: See currently active users
- `clear_firm_data`: Reset all firm data

**Guidelines:**
- Always check compliance mode before executing sensitive operations
- Track token usage for capacity planning
- Monitor active users for license management"""


class FirmDeploymentState(TypedDict):
    """State for firm deployment middleware."""


class FirmDeploymentMiddleware(AgentMiddleware[FirmDeploymentState, ContextT, ResponseT]):
    """Middleware for firm-wide deployment management.

    Provides tools for central configuration, usage analytics tracking,
    and active user management across the firm.
    """

    state_schema = FirmDeploymentState

    def __init__(self) -> None:
        self.store = FirmStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        """Build firm deployment tools."""
        mw = self

        def set_firm_config(
            runtime: ToolRuntime[None, FirmDeploymentState],
            firm_name: Annotated[str, "Name of the firm"] = "",
            allowed_models: Annotated[str, "Comma-separated list of allowed model identifiers"] = "",
            default_model: Annotated[str, "Default model identifier"] = "",
            compliance_mode: Annotated[str, "Enable compliance mode (true/false)"] = "",
            max_tokens_per_session: Annotated[int, "Maximum tokens per session"] = 0,
        ) -> str:
            """Configure firm-wide deployment settings."""
            models = [m.strip() for m in allowed_models.split(",") if m.strip()] if allowed_models else None
            comp = None
            if compliance_mode:
                comp = compliance_mode.lower() in ("true", "yes", "1", "on")
            tokens = max_tokens_per_session if max_tokens_per_session > 0 else None
            config = mw.store.set_config(
                firm_name=firm_name,
                allowed_models=models,
                default_model=default_model,
                compliance_mode=comp,
                max_tokens_per_session=tokens,
            )
            return f"Firm config updated: {config.firm_name or 'Unnamed'} | Compliance: {'ON' if config.compliance_mode else 'OFF'} | Max tokens: {config.max_tokens_per_session:,}"

        def record_usage(
            runtime: ToolRuntime[None, FirmDeploymentState],
            user_id: Annotated[str, "User identifier"],
            action: Annotated[str, "Action performed"],
            tokens_used: Annotated[int, "Number of tokens consumed"] = 0,
        ) -> str:
            """Record a usage event for analytics tracking."""
            record = mw.store.record_usage(user_id=user_id, action=action, tokens_used=tokens_used)
            return f"Recorded: {record.user_id} -> {record.action} ({record.tokens_used} tokens) at {record.timestamp}"

        def usage_analytics(
            runtime: ToolRuntime[None, FirmDeploymentState],
        ) -> str:
            """View aggregated usage analytics and statistics."""
            return mw.store.format_analytics()

        def list_active_users(
            runtime: ToolRuntime[None, FirmDeploymentState],
        ) -> str:
            """List currently active users."""
            if not mw.store.active_users:
                return "No active users."
            lines = [f"## Active Users ({len(mw.store.active_users)})", ""]
            for user_id in sorted(mw.store.active_users):
                user_records = [r for r in mw.store.usage_records if r.user_id == user_id]
                total_tokens = sum(r.tokens_used for r in user_records)
                last_action = user_records[-1].action if user_records else "N/A"
                lines.append(f"- **{user_id}**: {len(user_records)} events, {total_tokens:,} tokens, last: {last_action}")
            return "\n".join(lines)

        def clear_firm_data(
            runtime: ToolRuntime[None, FirmDeploymentState],
        ) -> str:
            """Clear all firm configuration and usage data."""
            records = len(mw.store.usage_records)
            users = len(mw.store.active_users)
            mw.store = FirmStore()
            return f"Cleared firm data: {records} usage records, {users} active users."

        return [
            StructuredTool.from_function(
                name="set_firm_config", description="Configure firm-wide deployment settings including models and compliance.", func=set_firm_config
            ),
            StructuredTool.from_function(name="record_usage", description="Record a usage event for analytics tracking.", func=record_usage),
            StructuredTool.from_function(
                name="usage_analytics", description="View aggregated usage statistics and configuration summary.", func=usage_analytics
            ),
            StructuredTool.from_function(
                name="list_active_users", description="List currently active users with usage summaries.", func=list_active_users
            ),
            StructuredTool.from_function(name="clear_firm_data", description="Clear all firm configuration and usage data.", func=clear_firm_data),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Inject firm deployment instructions.

        Args:
            request: Model request to modify.

        Returns:
            Modified request.
        """
        return request.override(system_message=append_to_system_message(request.system_message, FIRM_DEPLOYMENT_SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject firm deployment instructions.

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


__all__ = ["FirmConfig", "FirmDeploymentMiddleware", "FirmStore", "UsageRecord"]
