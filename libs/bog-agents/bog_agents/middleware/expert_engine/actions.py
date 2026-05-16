"""Action executor for the expert rule engine.

Actions are the side effects a rule emits when it fires. The executor
collects them, dispatches each to a small typed handler, and produces an
:class:`ActionResult` summarising the run. The middleware then translates
the result into agent-visible behaviour (deny a tool call, modify args,
escalate to ``ApprovalStore``, etc.).

Side effects that touch the outside world (Slack, audit log, approval
store) go through pluggable sinks so the engine itself is testable and
the production sink can be swapped per deployment.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bog_agents.middleware.expert_engine.matcher import resolve_value
from bog_agents.middleware.expert_engine.types import (
    Action,
    ActionKind,
    Activation,
    Fact,
    Trace,
)
from bog_agents.middleware.expert_engine.working_memory import WorkingMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ActionOutcome:
    """The result of executing one :class:`Action`.

    Attributes:
        kind: The :class:`ActionKind` that was executed.
        rule_name: Which rule fired this action.
        params: Resolved params (templates substituted).
        ok: True if the action ran without raising.
        message: Human-readable summary for the trace.
    """

    kind: ActionKind
    rule_name: str
    params: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    message: str = ""


@dataclass
class ActionResult:
    """Aggregate result returned from :meth:`ActionExecutor.execute_all`.

    Attributes:
        outcomes: Every action outcome in execution order.
        denied: True iff at least one :class:`ActionKind.DENY` fired.
            The caller (middleware) MUST block the underlying operation.
        deny_reasons: Reasons attached to deny actions, in fire order.
        modifications: Param dicts from :class:`ActionKind.MODIFY` actions
            in fire order. Last one wins for any overlapping key.
        approvals_required: Pending approval gates produced by
            :class:`ActionKind.REQUIRE_APPROVAL` actions.
        routes: Subagent routes produced by
            :class:`ActionKind.ROUTE_TO_SUBAGENT`.
        ask_llm: True iff at least one :class:`ActionKind.ASK_LLM` fired.
    """

    outcomes: list[ActionOutcome] = field(default_factory=list)
    denied: bool = False
    deny_reasons: list[str] = field(default_factory=list)
    modifications: list[dict[str, Any]] = field(default_factory=list)
    approvals_required: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)
    ask_llm: bool = False

    def merged_modification(self) -> dict[str, Any]:
        """Return the union of every modify-action params dict (last wins)."""
        merged: dict[str, Any] = {}
        for mod in self.modifications:
            merged.update(mod)
        return merged


# ---------------------------------------------------------------------------
# Sinks (pluggable side-effect handlers)
# ---------------------------------------------------------------------------

NotifySink = Callable[[str, dict[str, Any]], None]
"""``(channel, payload) -> None``. Default is a debug-level log."""

AuditSink = Callable[[str, dict[str, Any]], None]
"""``(event, payload) -> None``. Default is a debug-level log."""


def _default_notify(channel: str, payload: dict[str, Any]) -> None:
    logger.info("expert_engine.notify channel=%s payload=%s", channel, payload)


def _default_audit(event: str, payload: dict[str, Any]) -> None:
    logger.info("expert_engine.audit event=%s payload=%s", event, payload)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class ActionExecutor:
    """Execute the actions of one or more activations.

    Args:
        memory: The :class:`WorkingMemory` actions may mutate (assert /
            retract). Must be the same memory the engine is iterating.
        notify: Override the notification sink.
        audit: Override the audit log sink.
    """

    def __init__(
        self,
        memory: WorkingMemory,
        *,
        notify: NotifySink | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._memory = memory
        self._notify = notify or _default_notify
        self._audit = audit or _default_audit

    def execute_all(
        self,
        activations: list[Activation],
        trace: Trace,
    ) -> ActionResult:
        """Run every action of every activation, in salience-then-recency order.

        Args:
            activations: Activations to fire, pre-sorted by the engine.
            trace: Trace to append per-action events to.

        Returns:
            An :class:`ActionResult` aggregating all outcomes.
        """
        result = ActionResult()
        for act in activations:
            for action in act.rule.then:
                outcome = self._execute_one(action, act, trace)
                result.outcomes.append(outcome)
                self._apply_to_result(outcome, action, result)
        return result

    def execute_actions(
        self,
        rule_name: str,
        actions: tuple[Action, ...],
        bindings: dict[str, Fact],
        trace: Trace,
    ) -> ActionResult:
        """Execute a single action list with explicit bindings.

        Useful for backward-chaining "what would happen if I asserted X?"
        simulations where there is no real activation.
        """
        synthetic = Activation(
            rule=_SyntheticRule(name=rule_name),
            bindings=bindings,
            matched_facts=(),
        )
        result = ActionResult()
        for action in actions:
            outcome = self._execute_one(action, synthetic, trace)
            result.outcomes.append(outcome)
            self._apply_to_result(outcome, action, result)
        return result

    # ------------------------------------------------------------------
    # Individual actions
    # ------------------------------------------------------------------

    def _execute_one(
        self,
        action: Action,
        activation: Activation,
        trace: Trace,
    ) -> ActionOutcome:
        """Resolve params and dispatch a single action."""
        params = resolve_value(dict(action.params), activation.bindings)
        rule_name = activation.rule.name
        outcome = ActionOutcome(kind=action.kind, rule_name=rule_name, params=params)
        try:
            handler = _HANDLERS.get(action.kind)
            if handler is None:
                outcome.ok = False
                outcome.message = f"unknown action kind: {action.kind}"
            else:
                outcome.message = handler(self, params, activation)
        except Exception as exc:
            outcome.ok = False
            outcome.message = f"action error: {exc!s}"
            logger.exception("expert_engine.action_failed rule=%s kind=%s", rule_name, action.kind)
        trace.record(
            kind="action",
            rule_name=rule_name,
            detail=f"{action.kind.value} → {outcome.message}",
        )
        return outcome

    # Handlers ---------------------------------------------------------

    def _handle_deny(self, params: dict[str, Any], _activation: Activation) -> str:
        reason = str(params.get("reason", "denied by rule"))
        return f"deny: {reason}"

    def _handle_modify(self, params: dict[str, Any], _activation: Activation) -> str:
        return f"modify: {sorted(params.keys())}"

    def _handle_require_approval(self, params: dict[str, Any], _activation: Activation) -> str:
        gate = params.get("gate", "approval required")
        return f"approval: {gate}"

    def _handle_notify(self, params: dict[str, Any], _activation: Activation) -> str:
        channel = str(params.get("channel", "default"))
        self._notify(channel, params)
        return f"notified channel={channel}"

    def _handle_audit_log(self, params: dict[str, Any], activation: Activation) -> str:
        event = str(params.get("event", activation.rule.name))
        self._audit(event, params)
        return f"audit event={event}"

    def _handle_assert_fact(self, params: dict[str, Any], _activation: Activation) -> str:
        fact_type = params.get("fact_type")
        if not isinstance(fact_type, str) or not fact_type:
            return "assert: missing fact_type"
        data = params.get("data", {})
        if not isinstance(data, dict):
            return "assert: data must be a dict"
        fact = self._memory.assert_fact(Fact(fact_type=fact_type, data=dict(data)))
        return f"assert id={fact.id} type={fact_type}"

    def _handle_retract_fact(self, params: dict[str, Any], _activation: Activation) -> str:
        fact_id = params.get("fact_id")
        if isinstance(fact_id, int):
            removed = self._memory.retract(fact_id)
            return f"retract id={fact_id} ok={removed is not None}"
        fact_type = params.get("fact_type")
        if not isinstance(fact_type, str):
            return "retract: missing fact_id or fact_type"
        removed_list = self._memory.retract_matching(fact_type)
        return f"retract type={fact_type} count={len(removed_list)}"

    def _handle_route(self, params: dict[str, Any], _activation: Activation) -> str:
        agent = str(params.get("agent", "default"))
        return f"route → {agent}"

    def _handle_ask_llm(self, params: dict[str, Any], _activation: Activation) -> str:
        prompt = params.get("prompt", "")
        truncated = (prompt[:60] + "…") if isinstance(prompt, str) and len(prompt) > 60 else prompt
        return f"ask_llm: {truncated!r}"

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _apply_to_result(
        self,
        outcome: ActionOutcome,
        action: Action,
        result: ActionResult,
    ) -> None:
        """Fold *outcome* into the aggregate *result*."""
        if not outcome.ok:
            return
        if action.kind is ActionKind.DENY:
            result.denied = True
            result.deny_reasons.append(str(outcome.params.get("reason", "denied")))
        elif action.kind is ActionKind.MODIFY:
            result.modifications.append(outcome.params)
        elif action.kind is ActionKind.REQUIRE_APPROVAL:
            result.approvals_required.append(outcome.params)
        elif action.kind is ActionKind.ROUTE_TO_SUBAGENT:
            result.routes.append(outcome.params)
        elif action.kind is ActionKind.ASK_LLM:
            result.ask_llm = True


# ---------------------------------------------------------------------------
# Handler dispatch table
# ---------------------------------------------------------------------------


_HANDLERS: dict[ActionKind, Callable[[ActionExecutor, dict[str, Any], Activation], str]] = {
    ActionKind.DENY: ActionExecutor._handle_deny,
    ActionKind.MODIFY: ActionExecutor._handle_modify,
    ActionKind.REQUIRE_APPROVAL: ActionExecutor._handle_require_approval,
    ActionKind.NOTIFY: ActionExecutor._handle_notify,
    ActionKind.AUDIT_LOG: ActionExecutor._handle_audit_log,
    ActionKind.ASSERT_FACT: ActionExecutor._handle_assert_fact,
    ActionKind.RETRACT_FACT: ActionExecutor._handle_retract_fact,
    ActionKind.ROUTE_TO_SUBAGENT: ActionExecutor._handle_route,
    ActionKind.ASK_LLM: ActionExecutor._handle_ask_llm,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _SyntheticRule:
    """Stand-in rule for :meth:`ActionExecutor.execute_actions`."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name
