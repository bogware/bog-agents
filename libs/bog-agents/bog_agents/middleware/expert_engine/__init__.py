"""Expert Mode rule engine — forward + backward chaining for bog-agents.

A small production rule system that runs alongside the LLM agent. Facts are
asserted into a typed working memory; rules whose conditions match fire and
emit actions (`deny`, `modify`, `require_approval`, `notify`, `audit_log`,
`assert_fact`, `retract_fact`, `route_to_subagent`, `ask_llm`). Backward
chaining walks the rule graph to answer ``/why <fact>`` and ``/prove <goal>``.

The engine is deliberately small (~600-1000 LOC) and pure-Python. Pattern
matching is a linear scan over a fact-type index, which is microseconds for
realistic policy rulebooks (< 1k facts, < 100 rules). Rete is a future
optimisation; do not introduce it without a benchmark that requires it.

Modules:

* ``types`` — dataclasses (``Fact``, ``Pattern``, ``Predicate``, ``Rule``,
  ``Action``, ``Activation``, ``FireResult``, ``Trace``).
* ``working_memory`` — ``WorkingMemory`` (assert / retract / iterate).
* ``matcher`` — pattern matcher with variable binding across patterns.
* ``actions`` — ``ActionExecutor`` (executes the action vocabulary).
* ``engine`` — ``ExpertEngine`` (forward chaining, conflict resolution).
* ``backward`` — ``BackwardChainer`` (proof tree walker).
* ``loader`` — YAML rule loader with helpful error messages.
"""

from __future__ import annotations

from bog_agents.middleware.expert_engine.actions import (
    ActionExecutor,
    ActionOutcome,
    ActionResult,
    AuditSink,
    NotifySink,
)
from bog_agents.middleware.expert_engine.backward import (
    BackwardChainer,
    ProofNode,
    ProofTree,
)
from bog_agents.middleware.expert_engine.engine import (
    ExpertEngine,
    FireResult,
)
from bog_agents.middleware.expert_engine.lint import (
    LintFinding,
    LintReport,
    lint,
    render_report,
)
from bog_agents.middleware.expert_engine.loader import (
    RuleLoadError,
    load_rule_file,
    load_rules_from_dir,
)
from bog_agents.middleware.expert_engine.matcher import (
    Match,
    PatternMatcher,
)
from bog_agents.middleware.expert_engine.types import (
    Action,
    ActionKind,
    Activation,
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
    Trace,
    TraceEntry,
)
from bog_agents.middleware.expert_engine.working_memory import WorkingMemory

__all__ = [
    "Action",
    "ActionExecutor",
    "ActionKind",
    "ActionOutcome",
    "ActionResult",
    "Activation",
    "AuditSink",
    "BackwardChainer",
    "ExpertEngine",
    "Fact",
    "FireResult",
    "LintFinding",
    "LintReport",
    "Match",
    "NotifySink",
    "Pattern",
    "PatternMatcher",
    "Predicate",
    "PredicateOp",
    "ProofNode",
    "ProofTree",
    "Rule",
    "RuleLoadError",
    "Trace",
    "TraceEntry",
    "WorkingMemory",
    "lint",
    "load_rule_file",
    "load_rules_from_dir",
    "render_report",
]
