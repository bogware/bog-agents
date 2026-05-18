"""Forward-chaining engine for the expert rule system.

The engine wires :class:`WorkingMemory`, :class:`PatternMatcher`, and
:class:`ActionExecutor` together into a fixed-point loop:

1. Match every loaded rule against current memory.
2. Resolve conflicts (salience → recency of bound facts → rule name).
3. Fire each non-suppressed activation's actions.
4. If any action mutated memory, restart from (1). Stop when no new
   activations appear.

Cycle detection: a hard ``max_iterations`` cap (default 200) prevents
infinite loops if a malformed rulebook keeps asserting + matching the
same facts. The engine records a ``cycle`` trace entry and stops.

Once-flags: a rule with ``once=True`` fires at most one activation per
:meth:`run`. The first match wins (salience-ordered).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from bog_agents.middleware.expert_engine.actions import (
    ActionExecutor,
    ActionResult,
    AuditSink,
    NotifySink,
)
from bog_agents.middleware.expert_engine.matcher import PatternMatcher
from bog_agents.middleware.expert_engine.types import (
    Activation,
    Fact,
    Rule,
    Trace,
)
from bog_agents.middleware.expert_engine.working_memory import WorkingMemory

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ITERATIONS = 200

_DEFAULT_SLOW_RUN_WARN_MS = 50.0
"""V5: warn when one engine run exceeds this many milliseconds.

The matcher is O(P x F^k) in the number of rules, facts, and pattern
arity. At current scale (~100 rules, ~1000 facts) one run completes
in microseconds. As either grows we want a structured warning so the
team can defer optimization until a real customer signal arrives.

Override via the ``BOG_AGENTS_RULES_SLOW_WARN_MS`` env var. Set to
``0`` to disable.
"""


def _resolve_slow_warn_ms() -> float:
    raw = os.environ.get("BOG_AGENTS_RULES_SLOW_WARN_MS", "").strip()
    if not raw:
        return _DEFAULT_SLOW_RUN_WARN_MS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SLOW_RUN_WARN_MS
    return max(0.0, value)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class FireResult:
    """Aggregate result returned from :meth:`ExpertEngine.run`.

    Attributes:
        activations: Every activation that fired, in fire order.
        actions: Per-activation action result from :class:`ActionExecutor`.
        trace: The full :class:`Trace` for the run.
        iterations: How many fixed-point passes were run.
        truncated: True iff ``max_iterations`` was reached.
        elapsed_ms: Wall-clock time the run consumed, in milliseconds.
            Populated by :meth:`ExpertEngine.run`; the engine emits a
            structured warning when this exceeds the configured
            slow-run threshold (V5).
    """

    activations: list[Activation] = field(default_factory=list)
    actions: ActionResult = field(default_factory=ActionResult)
    trace: Trace = field(default_factory=Trace)
    iterations: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0

    @property
    def denied(self) -> bool:
        return self.actions.denied

    @property
    def deny_reasons(self) -> list[str]:
        return self.actions.deny_reasons


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ExpertEngine:
    """The bog-agents expert mode rule engine.

    Args:
        rules: The initial rulebook. Replaceable via :meth:`set_rules`.
        memory: Optional preconfigured :class:`WorkingMemory`. A fresh
            empty memory is created if omitted.
        max_iterations: Hard ceiling on fixed-point passes per
            :meth:`run`. Default 200.
        notify: Notification sink for ``notify`` actions.
        audit: Audit sink for ``audit_log`` actions.
    """

    def __init__(
        self,
        rules: Iterable[Rule] = (),
        *,
        memory: WorkingMemory | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        notify: NotifySink | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._rules: list[Rule] = list(rules)
        # ``or`` would fall through on an empty WorkingMemory (truthiness
        # is driven by __len__, which is 0 for a fresh memory). Use an
        # explicit None check so a caller-provided empty memory is
        # honoured. Was a real bug in /expert write replay (Wave D).
        self._memory = memory if memory is not None else WorkingMemory()
        self._matcher = PatternMatcher()
        self._executor = ActionExecutor(self._memory, notify=notify, audit=audit)
        self._max_iterations = max(1, int(max_iterations))
        # Suppression sets used per-run; reset by ``run``.
        self._once_fired: set[str] = set()
        self._activation_history: set[tuple[str, tuple[int, ...]]] = set()
        self.last_result: FireResult | None = None

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def memory(self) -> WorkingMemory:
        return self._memory

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def set_rules(self, rules: Iterable[Rule]) -> None:
        """Replace the rulebook. Working memory is untouched."""
        self._rules = list(rules)

    def add_rule(self, rule: Rule) -> None:
        """Append a rule to the rulebook."""
        self._rules.append(rule)

    def assert_fact(self, fact: Fact) -> Fact:
        """Add a fact to the working memory. Does not run the engine."""
        return self._memory.assert_fact(fact)

    def retract(self, fact_id: int) -> Fact | None:
        return self._memory.retract(fact_id)

    # ------------------------------------------------------------------
    # Forward chaining
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        max_iterations: int | None = None,
        on_activation: Callable[[Activation], None] | None = None,
    ) -> FireResult:
        """Run the engine to a fixed point.

        Args:
            max_iterations: Override the per-run iteration ceiling.
            on_activation: Optional callback fired for every activation,
                immediately before its actions execute.

        Returns:
            A :class:`FireResult`.
        """
        limit = max_iterations if max_iterations is not None else self._max_iterations
        self._once_fired.clear()
        self._activation_history.clear()
        result = FireResult()
        # V5: time the whole run so we can warn on slow rulebooks
        # without paying for fine-grained instrumentation in the hot
        # path. ``time.perf_counter`` is the right clock on every
        # supported platform.
        started_at = time.perf_counter()
        for iteration in range(1, limit + 1):
            result.iterations = iteration
            activations = self._collect_activations(result.trace)
            if not activations:
                break
            stop = False
            for act in activations:
                # A once-rule may have fired earlier in the same iteration's
                # conflict set; suppress subsequent activations of the same rule.
                if act.rule.once and act.rule.name in self._once_fired:
                    continue
                result.activations.append(act)
                if on_activation is not None:
                    on_activation(act)
                action_result = self._executor.execute_all([act], result.trace)
                # Merge into aggregate action result
                _merge_action_results(result.actions, action_result)
                result.trace.record(
                    kind="fire",
                    rule_name=act.rule.name,
                    detail=f"fired with {len(act.matched_facts)} matched facts",
                )
                if act.rule.once:
                    self._once_fired.add(act.rule.name)
                if action_result.denied:
                    stop = True
                    break
            if stop:
                break
        else:
            result.truncated = True
            result.trace.record(
                kind="cycle",
                detail=f"max_iterations={limit} hit",
            )
        # V5: emit a structured warning if the run took longer than
        # the configured threshold. The threshold can be lowered for
        # CI runs (set BOG_AGENTS_RULES_SLOW_WARN_MS=10) or disabled
        # entirely with =0. The matcher itself stays O(P · F^k); this
        # is an observability hook, not an optimization.
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        threshold = _resolve_slow_warn_ms()
        if threshold > 0 and elapsed_ms > threshold:
            logger.warning(
                "expert_engine slow run: %.1fms over %d rule(s) + %d fact(s) "
                "(threshold %.0fms). Iterations=%d, activations=%d. "
                "Set BOG_AGENTS_RULES_SLOW_WARN_MS to retune.",
                elapsed_ms,
                len(self._rules),
                len(self._memory),
                threshold,
                result.iterations,
                len(result.activations),
            )
        result.elapsed_ms = elapsed_ms
        self.last_result = result
        return result

    # ------------------------------------------------------------------
    # Internal — conflict set construction
    # ------------------------------------------------------------------

    def _collect_activations(self, trace: Trace) -> list[Activation]:
        """Build, dedupe, and sort the conflict set for one iteration."""
        conflict_set: list[Activation] = []
        for rule in self._rules:
            if rule.once and rule.name in self._once_fired:
                continue
            for match in self._matcher.match_all(rule.when, self._memory):
                activation = Activation(
                    rule=rule,
                    bindings=match.bindings,
                    matched_facts=match.matched_facts,
                )
                sig = activation.signature
                if sig in self._activation_history:
                    continue
                self._activation_history.add(sig)
                conflict_set.append(activation)
                trace.record(
                    kind="activate",
                    rule_name=rule.name,
                    detail=f"matched {len(match.matched_facts)} facts",
                )
        conflict_set.sort(key=_conflict_key)
        return conflict_set


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conflict_key(act: Activation) -> tuple[int, int, str]:
    """Sort key for conflict resolution.

    Order: highest salience first, then most-recent matched fact first,
    then rule name (stable, alphabetical).
    """
    most_recent_fact_id = max((f.id for f in act.matched_facts), default=0)
    return (-act.rule.salience, -most_recent_fact_id, act.rule.name)


def _merge_action_results(target: ActionResult, src: ActionResult) -> None:
    """Fold *src* into *target* (used to roll up per-activation outcomes)."""
    target.outcomes.extend(src.outcomes)
    target.deny_reasons.extend(src.deny_reasons)
    target.modifications.extend(src.modifications)
    target.approvals_required.extend(src.approvals_required)
    target.routes.extend(src.routes)
    if src.denied:
        target.denied = True
    if src.ask_llm:
        target.ask_llm = True
