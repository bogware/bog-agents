"""Automations middleware for event-driven triggers.

Agents can create triggers and automation rules that fire on events such as
price alerts, schedules, filing detections, and threshold breaches.
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

EVENT_TYPES = ["price_alert", "schedule", "filing_detected", "threshold_breach", "custom"]


@dataclass
class TriggerCondition:
    """A trigger condition that can fire an automation."""

    trigger_id: str
    event_type: str
    condition: str
    threshold: float
    is_active: bool = True


@dataclass
class AutomationRule:
    """An automation rule linking a trigger to an action."""

    rule_id: str
    name: str
    trigger: TriggerCondition
    action_description: str
    last_triggered: str = ""
    trigger_count: int = 0


@dataclass
class AutomationStore:
    """In-memory store for automation rules."""

    rules: list[AutomationRule] = field(default_factory=list)
    _next_rule_id: int = 1
    _next_trigger_id: int = 1


SYSTEM_PROMPT = """You have access to automation and event-driven trigger tools. You can:
- Create triggers based on event types: price_alert, schedule, filing_detected, threshold_breach, custom
- Create automation rules that link triggers to actions
- List all active automations and their status
- Fire triggers manually to test automation workflows
Use these tools to set up automated monitoring and alerting for financial advisory tasks."""


class AutomationsState(TypedDict):
    """State for the automations middleware."""


class AutomationsMiddleware(AgentMiddleware[AutomationsState, ContextT, ResponseT]):
    """Middleware for event-driven automation triggers."""

    state_schema = AutomationsState

    def __init__(self) -> None:
        self.store = AutomationStore()
        self.tools: list[BaseTool] = self._build_tools()

    def _build_tools(self) -> list[BaseTool]:
        mw = self

        def create_trigger(
            runtime: ToolRuntime[None, AutomationsState],
            event_type: Annotated[str, "Type of event: price_alert, schedule, filing_detected, threshold_breach, custom"],
            condition: Annotated[str, "Description of the trigger condition"],
            threshold: Annotated[float, "Numeric threshold for the trigger"],
        ) -> str:
            """Create a new trigger condition for an automation."""
            if event_type not in EVENT_TYPES:
                return f"Error: Invalid event type '{event_type}'. Must be one of: {', '.join(EVENT_TYPES)}"
            tid = f"trig-{mw.store._next_trigger_id}"
            mw.store._next_trigger_id += 1
            trigger = TriggerCondition(
                trigger_id=tid,
                event_type=event_type,
                condition=condition,
                threshold=threshold,
            )
            logger.info("Created trigger %s (%s)", tid, event_type)
            return f"Created trigger '{tid}' ({event_type}): {condition} [threshold={threshold}]"

        def create_automation(
            runtime: ToolRuntime[None, AutomationsState],
            name: Annotated[str, "Name of the automation rule"],
            event_type: Annotated[str, "Type of event: price_alert, schedule, filing_detected, threshold_breach, custom"],
            condition: Annotated[str, "Description of the trigger condition"],
            threshold: Annotated[float, "Numeric threshold for the trigger"],
            action_description: Annotated[str, "Description of the action to take when triggered"],
        ) -> str:
            """Create a complete automation rule with a trigger and action."""
            if event_type not in EVENT_TYPES:
                return f"Error: Invalid event type '{event_type}'. Must be one of: {', '.join(EVENT_TYPES)}"
            tid = f"trig-{mw.store._next_trigger_id}"
            mw.store._next_trigger_id += 1
            trigger = TriggerCondition(
                trigger_id=tid,
                event_type=event_type,
                condition=condition,
                threshold=threshold,
            )
            rid = f"rule-{mw.store._next_rule_id}"
            mw.store._next_rule_id += 1
            rule = AutomationRule(
                rule_id=rid,
                name=name,
                trigger=trigger,
                action_description=action_description,
            )
            mw.store.rules.append(rule)
            logger.info("Created automation %s: %s", rid, name)
            return f"Created automation '{name}' (ID: {rid}) with trigger '{tid}' ({event_type})"

        def list_automations(
            runtime: ToolRuntime[None, AutomationsState],
        ) -> str:
            """List all automation rules and their status."""
            if not mw.store.rules:
                return "No automations configured."
            lines = [f"Automations ({len(mw.store.rules)}):"]
            for rule in mw.store.rules:
                status = "active" if rule.trigger.is_active else "inactive"
                lines.append(f"  - {rule.rule_id}: {rule.name} [{status}] (triggered {rule.trigger_count}x) -> {rule.action_description}")
            return "\n".join(lines)

        def fire_trigger(
            runtime: ToolRuntime[None, AutomationsState],
            rule_id: Annotated[str, "ID of the automation rule to fire"],
        ) -> str:
            """Manually fire a trigger for testing purposes."""
            for rule in mw.store.rules:
                if rule.rule_id == rule_id:
                    if not rule.trigger.is_active:
                        return f"Error: Trigger for rule '{rule_id}' is inactive."
                    rule.trigger_count += 1
                    rule.last_triggered = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())
                    logger.info("Fired trigger for rule %s", rule_id)
                    return f"Fired trigger for '{rule.name}' ({rule_id}). Action: {rule.action_description}. Total triggers: {rule.trigger_count}"
            return f"Error: Rule '{rule_id}' not found."

        def clear_automations(
            runtime: ToolRuntime[None, AutomationsState],
        ) -> str:
            """Clear all automation rules and triggers."""
            count = len(mw.store.rules)
            mw.store = AutomationStore()
            logger.info("Cleared %d automation rules", count)
            return f"Cleared {count} automation rule(s)."

        return [
            StructuredTool.from_function(create_trigger, name="create_trigger"),
            StructuredTool.from_function(create_automation, name="create_automation"),
            StructuredTool.from_function(list_automations, name="list_automations"),
            StructuredTool.from_function(fire_trigger, name="fire_trigger"),
            StructuredTool.from_function(clear_automations, name="clear_automations"),
        ]

    def modify_request(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the automations system prompt to the request."""
        return request.override(system_message=append_to_system_message(request.system_message, SYSTEM_PROMPT))

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Synchronously wrap the model call with automations context."""
        return call_next(self.modify_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Asynchronously wrap the model call with automations context."""
        return await call_next(self.modify_request(request))


__all__ = ["AutomationRule", "AutomationStore", "AutomationsMiddleware", "TriggerCondition"]
