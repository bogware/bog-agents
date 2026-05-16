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
        middleware=[ExpertRulesMiddleware(working_dir=Path(".") )],
    )
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

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
    load_rules_from_dir,
)

logger = logging.getLogger(__name__)


_DEFAULT_RULES_DIR = ".bog-agents/expert_rules"
_DEFAULT_RELOAD_INTERVAL = 30.0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


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
            params dict.
        extra_rules: Rules to load programmatically (additive over disk).
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
        extra_rules: list[Rule] | None = None,
    ) -> None:
        self._working_dir = working_dir or Path.cwd()
        self._rules_subdir = rules_subdir
        self._reload_interval = reload_interval
        self._enabled = enabled
        self._extra_rules: list[Rule] = list(extra_rules or [])
        self._engine = ExpertEngine(
            self._extra_rules,
            max_iterations=max_iterations,
            notify=notify,
            audit=audit,
        )
        self._on_approval = on_approval_required
        self._last_loaded: float = 0.0
        self._last_load_error: str = ""
        self._denials = 0
        self._modifications = 0
        self._approvals = 0
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
        fact = Fact(
            fact_type="tool_call",
            data={
                "name": tool_call.get("name", ""),
                "args": dict(args),
                "id": tool_call.get("id", ""),
                # Convenience flatten for common shell-execute pattern:
                "command": args.get("command", "") if isinstance(args, dict) else "",
            },
        )
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
            if self._on_approval is not None:
                for params in result.actions.approvals_required:
                    try:
                        self._on_approval(params)
                    except Exception:
                        logger.exception("expert_rules: approval callback failed")
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
    # ``ToolCallRequest`` is a dataclass with ``__replace__`` (Python 3.13+).
    # Fall back to construction if needed.
    if hasattr(request, "__replace__"):
        return cast(ToolCallRequest, request.__replace__(tool_call=new_call))
    return ToolCallRequest(
        tool_call=new_call,
        tool=request.tool,
        state=request.state,
        runtime=request.runtime,
    )
