"""Expert Mode middleware — wraps the rule engine into bog-agents.

This middleware loads YAML rules from ``<project>/.bog-agents/expert_rules/``
and runs the engine before every tool call. The engine can deny (block the
call), modify (rewrite ``tool_call["args"]``), or require approval (escalate
to :class:`ApprovalStore`). When neither denied nor modified, the call
proceeds unchanged so the LLM stays the default decision-maker.

Designed to compose with the existing :class:`RulesMiddleware` (which
injects prose rules into the system prompt). They serve different
purposes and can both be active — one constrains output, the other guides
generation.

Example::

    from bog_agents.middleware.expert_rules import ExpertRulesMiddleware

    agent = create_agent(
        model="claude-opus-4-7",
        middleware=[ExpertRulesMiddleware(working_dir=Path("."))],
    )
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from typing_extensions import TypedDict

from bog_agents.middleware.expert_engine import (
    AuditSink,
    BackwardChainer,
    ExpertEngine,
    Fact,
    FireResult,
    NotifySink,
    Pattern,
    Rule,
    RuleLoadError,
    WorkingMemory,
    load_rules_from_dir,
)

if TYPE_CHECKING:
    from bog_agents.middleware.approval_gates import ApprovalStore

logger = logging.getLogger(__name__)


_DEFAULT_RULES_DIR = ".bog-agents/expert_rules"
_DEFAULT_RELOAD_INTERVAL = 30.0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


__all__ = [
    "ExpertRulesMiddleware",
    "ExpertRulesState",
]


class ExpertRulesState(TypedDict, total=False):
    """LangGraph state for the expert rules middleware.

    The middleware itself is mostly stateless — the engine carries its
    working memory in process. We only surface counters here so
    observability tools / slash commands can read them out of state.
    """

    expert_rules_denials: int
    expert_rules_modifications: int
    expert_rules_approvals: int


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ExpertRulesMiddleware(AgentMiddleware[ExpertRulesState, ContextT, ResponseT]):
    """Run a forward-chaining rule engine before every tool call.

    Args:
        working_dir: Project root. Rules are loaded from
            ``<working_dir>/.bog-agents/expert_rules/*.yaml``.
        rules_subdir: Override the rules directory name.
        reload_interval: Seconds between rule reloads. ``0`` disables
            reloading.
        enabled: Soft switch — when False, the middleware passes calls
            through unchanged (used by ``/expert off`` from the CLI).
        max_iterations: Per-tool-call engine iteration cap.
        notify: Optional notification sink (``(channel, payload) -> None``).
        audit: Optional audit log sink.
        on_approval_required: Callback fired when a rule's
            ``require_approval`` action triggers. Receives the resolved
            params dict. Pure observer — does not block.
        approval_store: Optional :class:`ApprovalStore` from
            :mod:`bog_agents.middleware.approval_gates`. When supplied,
            every ``require_approval`` action creates a real submission
            on the store (auto-creating the gate if it doesn't exist)
            so downstream review UIs / hooks can pick it up. Without a
            store the middleware still blocks the tool call but the
            approval lives only in the engine's return value.
        extra_rules: Rules to load programmatically (additive over disk).
        max_working_facts: Soft cap on the number of *derived* facts
            (everything except the per-call ``tool_call`` structural fact)
            held in the shared engine memory. Crossing it logs a one-time
            warning; running far past it FIFO-evicts the oldest derived
            facts so a long-lived daemon session can't leak memory or
            compound matcher latency. ``0`` disables the cap.
    """

    state_schema = ExpertRulesState

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        rules_subdir: str = _DEFAULT_RULES_DIR,
        reload_interval: float = _DEFAULT_RELOAD_INTERVAL,
        enabled: bool = True,
        max_iterations: int = 200,
        notify: NotifySink | None = None,
        audit: AuditSink | None = None,
        on_approval_required: Callable[[dict[str, Any]], None] | None = None,
        approval_store: ApprovalStore | None = None,
        extra_rules: list[Rule] | None = None,
        max_working_facts: int = 5000,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._rules_subdir = rules_subdir
        self._reload_interval = reload_interval
        self._enabled = enabled
        self._extra_rules: list[Rule] = list(extra_rules or [])
        # Bound the shared engine memory. Rule ``assert_fact`` actions add
        # derived facts that are never retracted (only the per-call
        # ``tool_call`` structural fact is), so over a long daemon session
        # they leak memory and compound matcher latency. The soft cap warns
        # once on crossing and FIFO-evicts the oldest derived facts far past
        # it, leaving ``tool_call`` (and other structural) facts untouched so
        # cross-call rule semantics keep working.
        self._engine = ExpertEngine(
            self._extra_rules,
            memory=WorkingMemory(max_working_facts=max_working_facts),
            max_iterations=max_iterations,
            notify=notify,
            audit=audit,
        )
        self._on_approval = on_approval_required
        self._approval_store = approval_store
        self._last_loaded: float = 0.0
        self._last_load_error: str = ""
        self._denials = 0
        self._modifications = 0
        self._approvals = 0
        # Bounded ring buffer of recent tool_call data dicts. Used by
        # /expert write to replay LLM-proposed rules against real
        # historical calls (Wave D, REVIEW.md T-11 v2 #4). 200 is
        # plenty for an hours-long session without bloating memory.
        self._tool_call_history: deque[dict[str, Any]] = deque(maxlen=200)
        # Eagerly load once so the first tool call doesn't pay a stat() cost
        # on disk inside the hot path.
        self._reload_rules(force=True)

    # ------------------------------------------------------------------
    # Public surface (used by slash commands)
    # ------------------------------------------------------------------

    @property
    def engine(self) -> ExpertEngine:
        return self._engine

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)

    def reload(self) -> tuple[int, str]:
        """Force a rule reload. Returns ``(rule_count, error_or_empty)``."""
        self._reload_rules(force=True)
        return (len(self._engine.rules), self._last_load_error)

    @property
    def counters(self) -> dict[str, int]:
        return {
            "denials": self._denials,
            "modifications": self._modifications,
            "approvals": self._approvals,
        }

    def make_backward_chainer(self) -> BackwardChainer:
        """Construct a :class:`BackwardChainer` over the live engine."""
        return BackwardChainer(self._engine.rules, self._engine.memory)

    def explain(self, pattern: Pattern) -> dict[str, Any]:
        """JSON-renderable proof tree for ``/why`` slash command."""
        return self.make_backward_chainer().why(pattern).to_dict()

    def prove(self, pattern: Pattern) -> dict[str, Any]:
        """JSON-renderable proof tree for ``/prove`` slash command."""
        return self.make_backward_chainer().prove(pattern).to_dict()

    def last_trace(self) -> list[dict[str, Any]]:
        """Return the entries of the last run trace, JSON-ready."""
        result = self._engine.last_result
        if result is None:
            return []
        return [
            {
                "kind": e.kind,
                "rule": e.rule_name,
                "detail": e.detail,
                "fact_id": e.fact_id,
                "fact_type": e.fact_type,
                "at": e.at,
            }
            for e in result.trace.entries
        ]

    # ------------------------------------------------------------------
    # Tool-call interception
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """Sync hook: run engine, deny/modify/pass-through."""
        if not self._enabled:
            return handler(request)
        self._ensure_loaded()
        result = self._run_for_request(request)
        decision = self._apply_decision(request, result)
        if decision is _DENY:
            return self._make_deny_message(request, result)
        if decision is _APPROVAL:
            return self._make_approval_message(request, result)
        # decision is either _PASS (no change) or a modified ToolCallRequest
        new_request = decision if isinstance(decision, ToolCallRequest) else request
        return handler(new_request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """Async hook: run engine, deny/modify/pass-through."""
        if not self._enabled:
            return await handler(request)
        self._ensure_loaded()
        result = self._run_for_request(request)
        decision = self._apply_decision(request, result)
        if decision is _DENY:
            return self._make_deny_message(request, result)
        if decision is _APPROVAL:
            return self._make_approval_message(request, result)
        new_request = decision if isinstance(decision, ToolCallRequest) else request
        return await handler(new_request)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._reload_interval <= 0:
            return
        now = time.monotonic()
        if now - self._last_loaded > self._reload_interval:
            self._reload_rules(force=False)

    def _reload_rules(self, *, force: bool) -> None:
        rules_dir = self._working_dir / self._rules_subdir
        try:
            disk = load_rules_from_dir(rules_dir)
            self._last_load_error = ""
            logger.debug(
                "expert_rules: loaded %d rules from %s",
                len(disk),
                rules_dir,
            )
        except RuleLoadError as exc:
            # Keep the previous rule set live so a broken file doesn't open the gates.
            self._last_load_error = str(exc)
            logger.warning("expert_rules: rule load failed (%s)", exc)
            if not force:
                # Re-arm the throttle so we don't hammer disk on every tool call.
                self._last_loaded = time.monotonic()
            return
        merged: list[Rule] = [*disk, *self._extra_rules]
        self._engine.set_rules(merged)
        self._last_loaded = time.monotonic()

    def _run_for_request(self, request: ToolCallRequest) -> FireResult:
        tool_call = request.tool_call or {}
        # Build a tool_call fact with the request shape rule authors expect.
        # ``args`` is a dict in modern langchain; coerce to JSON-friendly types.
        args = tool_call.get("args") or {}
        data = {
            "name": tool_call.get("name", ""),
            "args": dict(args),
            "id": tool_call.get("id", ""),
            # Convenience flatten for common shell-execute pattern:
            "command": args.get("command", "") if isinstance(args, dict) else "",
            # String view of ALL args so `matches`/`contains` predicates can
            # scan the whole arg blob (the bare `args` field is a dict and a
            # string-regex predicate never matches it). (REVIEW.md v2 P1-20.)
            "args_text": json.dumps(args, default=str, ensure_ascii=False),
        }
        # Record into the ring buffer BEFORE asserting so /expert write
        # replay (Wave D) can see every call the agent has made, not
        # just the ones rules fired against.
        self._tool_call_history.append(dict(data))
        fact = Fact(fact_type="tool_call", data=data)
        # The engine's memory is shared across calls; the caller can flush it
        # via ``engine.memory.clear()`` if needed. By default we let it grow
        # so cross-call rules (rate-limiting, cumulative cost) work.
        asserted = self._engine.assert_fact(fact)
        try:
            return self._engine.run()
        finally:
            # Retract the tool_call fact after the run so the next call doesn't
            # double-match. The fact is preserved in the trace.
            self._engine.retract(asserted.id)

    @property
    def tool_call_history(self) -> list[dict[str, Any]]:
        """Return a snapshot of the recent tool_call data (oldest first).

        Used by the /expert write authoring flow (REVIEW.md T-11 v2 #4)
        to replay LLM-proposed rules against real calls the agent has
        made this session.
        """
        return list(self._tool_call_history)

    def _apply_decision(
        self,
        request: ToolCallRequest,
        result: FireResult,
    ) -> ToolCallRequest | object:
        """Translate the engine result into a control-flow decision.

        Returns one of: :data:`_DENY`, :data:`_APPROVAL`, :data:`_PASS`,
        or a new :class:`ToolCallRequest` with modified args.
        """
        if result.denied:
            self._denials += 1
            return _DENY
        if result.actions.approvals_required:
            self._approvals += 1
            for params in result.actions.approvals_required:
                # Fire observer callback first so a watcher sees every gate.
                if self._on_approval is not None:
                    try:
                        self._on_approval(params)
                    except Exception:
                        logger.exception("expert_rules: approval callback failed")
                # Create a real submission on the store if one was supplied.
                # Auto-create the gate if missing (the engine names them by
                # the ``gate`` param so downstream reviewers see the
                # rule-author's wording rather than a synthetic id).
                if self._approval_store is not None:
                    gate_name = str(params.get("gate") or "expert-rule")
                    risk = str(params.get("risk") or params.get("severity") or "medium")
                    action_desc = str(
                        params.get("reason") or params.get("description") or f"expert rule fired: {gate_name}",
                    )
                    try:
                        if gate_name not in self._approval_store.gates:
                            self._approval_store.create_gate(
                                name=gate_name,
                                required_approvers=int(params.get("required_approvers", 1)),
                                description=action_desc,
                            )
                        self._approval_store.submit(
                            gate_name=gate_name,
                            action_description=action_desc,
                            risk_level=risk,
                        )
                    except Exception:
                        logger.exception("expert_rules: failed to register approval submission on approval_store")
            return _APPROVAL
        mods = result.actions.merged_modification()
        if mods:
            self._modifications += 1
            return _request_with_modified_args(request, mods)
        return _PASS

    def _make_deny_message(
        self,
        request: ToolCallRequest,
        result: FireResult,
    ) -> ToolMessage:
        """Produce a ToolMessage explaining the deny."""
        reasons = result.deny_reasons or ["denied by expert rule"]
        body = {
            "expert_rules": "deny",
            "reasons": reasons,
            "fired_rules": [a.rule.name for a in result.activations],
        }
        return ToolMessage(
            content=json.dumps(body),
            tool_call_id=str(request.tool_call.get("id", "")),
            name=str(request.tool_call.get("name", "")),
            status="error",
        )

    def _make_approval_message(
        self,
        request: ToolCallRequest,
        result: FireResult,
    ) -> ToolMessage:
        """Produce a ToolMessage indicating approval is required."""
        body = {
            "expert_rules": "approval_required",
            "gates": result.actions.approvals_required,
            "fired_rules": [a.rule.name for a in result.activations],
        }
        return ToolMessage(
            content=json.dumps(body),
            tool_call_id=str(request.tool_call.get("id", "")),
            name=str(request.tool_call.get("name", "")),
            status="error",
        )


# ---------------------------------------------------------------------------
# Decision sentinels
# ---------------------------------------------------------------------------


class _Decision:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return f"<{self.name}>"


_DENY = _Decision("DENY")
_APPROVAL = _Decision("APPROVAL")
_PASS = _Decision("PASS")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_with_modified_args(
    request: ToolCallRequest,
    modifications: dict[str, Any],
) -> ToolCallRequest:
    """Return a new :class:`ToolCallRequest` with ``tool_call.args`` updated.

    The modify-action params overlay onto the existing args (last write wins).
    Other tool_call fields (name, id) are preserved.
    """
    old_call = request.tool_call or {}
    old_args = old_call.get("args") or {}
    new_args = {**old_args, **modifications}
    new_call = {**old_call, "args": new_args}
    # ``dataclasses.replace`` is the public-API equivalent of the
    # ``__replace__`` dunder added in Python 3.13. Using the function
    # form keeps the typing predictable (``ty`` sees ``__replace__`` as
    # ``object``, not a callable) and preserves every other field on
    # ``ToolCallRequest`` even if upstream adds new ones we haven't
    # listed in the fallback constructor.
    if dataclasses.is_dataclass(request):
        return dataclasses.replace(request, tool_call=new_call)
    return ToolCallRequest(
        tool_call=new_call,
        tool=request.tool,
        state=request.state,
        runtime=request.runtime,
    )
