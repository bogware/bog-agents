"""Core data model for the expert rule engine.

All types are frozen dataclasses where practical, so they hash and compare
by value. The exception is :class:`Trace` and :class:`TraceEntry`, which
collect activity and need to stay mutable for the duration of a run.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PredicateOp(str, Enum):
    """Comparison operators understood by :class:`Predicate`."""

    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    MATCHES = "matches"  # regex search on string
    CONTAINS = "contains"  # substring or list-contains
    EXISTS = "exists"  # field is present (value is ignored)
    MISSING = "missing"  # field is not present


class ActionKind(str, Enum):
    """The action vocabulary the engine can execute when a rule fires."""

    DENY = "deny"
    """Block the current tool call / decision. Rule wins."""

    MODIFY = "modify"
    """Rewrite the tool-call arguments. Engine sets ``replacement_input``."""

    REQUIRE_APPROVAL = "require_approval"
    """Pause for human approval before proceeding."""

    NOTIFY = "notify"
    """Side-channel notification (Slack, email, etc.). Non-blocking."""

    AUDIT_LOG = "audit_log"
    """Write a structured event to the audit trail."""

    ASSERT_FACT = "assert_fact"
    """Add a new fact to working memory (drives forward chaining)."""

    RETRACT_FACT = "retract_fact"
    """Remove a fact from working memory."""

    ROUTE_TO_SUBAGENT = "route_to_subagent"
    """Hand the request off to a named subagent."""

    ASK_LLM = "ask_llm"
    """Escape hatch — defer to the LLM with the matched facts as context."""


# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """A single typed fact in working memory.

    Facts are immutable. Retraction is by stable ``id``, not by mutation.

    Attributes:
        fact_type: Discriminator — e.g. ``"tool_call"``, ``"session"``,
            ``"file_edit"``. The engine indexes by this for fast retrieval.
        data: Arbitrary field map. Predicates look up keys here.
        id: Stable identifier set by :class:`WorkingMemory` on assertion.
            ``0`` indicates a fact that has not yet been asserted.
        asserted_at: ``time.monotonic()`` at assertion time. Used by the
            recency tie-breaker in conflict resolution.
    """

    fact_type: str
    data: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    id: int = 0
    asserted_at: float = 0.0

    def get(self, key: str, default: Any = None) -> Any:
        """Return ``data[key]`` if present, else ``default``."""
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Predicate + Pattern
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Predicate:
    """A single test against one field of a fact.

    Attributes:
        field: Key in ``Fact.data`` to test. Dotted paths like ``"user.id"``
            walk into nested dicts.
        op: How to compare. See :class:`PredicateOp`.
        value: Right-hand side. For :attr:`PredicateOp.EXISTS` /
            :attr:`PredicateOp.MISSING` the value is ignored.
    """

    field: str
    op: PredicateOp
    value: Any = None

    def test(self, fact: Fact) -> bool:
        """Return ``True`` if this predicate holds for *fact*."""
        actual = _walk(fact.data, self.field)
        present = actual is not _MISSING
        if self.op is PredicateOp.EXISTS:
            return present
        if self.op is PredicateOp.MISSING:
            return not present
        if not present:
            return False
        return _COMPARE[self.op](actual, self.value)


@dataclass(frozen=True)
class Pattern:
    """A pattern that matches a fact of a particular type plus predicates.

    Attributes:
        fact_type: Required ``Fact.fact_type`` to match.
        predicates: All predicates must hold (logical AND).
        bind: Optional variable name. When set, the matched fact is bound
            to that name so later patterns / actions can reference it.
        negated: When True, the overall match succeeds **iff no fact of
            this shape exists** (negation as failure / classical "not").
    """

    fact_type: str
    predicates: tuple[Predicate, ...] = ()
    bind: str | None = None
    negated: bool = False

    def matches_fact(self, fact: Fact) -> bool:
        """Test all predicates against a single candidate fact."""
        if fact.fact_type != self.fact_type:
            return False
        return all(p.test(fact) for p in self.predicates)


# ---------------------------------------------------------------------------
# Action + Rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """A single action to execute when a rule fires.

    Attributes:
        kind: Which verb to run. See :class:`ActionKind`.
        params: Parameters for the action. Strings may use ``{{var}}``
            templating which is filled in from the matched variable
            bindings at fire time.
    """

    kind: ActionKind
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class Rule:
    """A production rule.

    Attributes:
        name: Unique identifier used in traces and ``/why`` queries.
        when: All patterns must match (logical AND). An empty ``when``
            tuple matches once per run regardless of facts.
        then: Actions to execute, in order, when ``when`` matches.
        salience: Higher salience fires first. Default 0.
        once: When True, the rule fires at most once per engine ``run``.
        description: Human-readable, used by ``/explain``.
        source_file: Optional path to the YAML file this rule came from
            (for error messages and ``/rules show``).
    """

    name: str
    when: tuple[Pattern, ...] = ()
    then: tuple[Action, ...] = ()
    salience: int = 0
    once: bool = False
    description: str = ""
    source_file: str = ""


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Activation:
    """A single rule-instance that is ready to fire.

    Attributes:
        rule: The rule whose conditions matched.
        bindings: Variable name → matched fact. Empty for negated /
            no-binding patterns.
        matched_facts: All facts (in pattern order) that produced this
            activation, used for trace + dependency tracking.
    """

    rule: Rule
    bindings: dict[str, Fact] = field(default_factory=dict, hash=False, compare=False)
    matched_facts: tuple[Fact, ...] = ()

    @property
    def signature(self) -> tuple[str, tuple[int, ...]]:
        """Stable identity for activation dedup: rule name + matched-fact ids."""
        return (self.rule.name, tuple(f.id for f in self.matched_facts))


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


@dataclass
class TraceEntry:
    """A single event in the engine's run log."""

    kind: str  # 'assert' | 'retract' | 'fire' | 'skip' | 'action' | 'cycle'
    rule_name: str = ""
    fact_id: int = 0
    fact_type: str = ""
    detail: str = ""
    at: float = field(default_factory=time.monotonic)


@dataclass
class Trace:
    """An ordered log of events during one engine run.

    Mutable. Append entries via :meth:`record`. The engine exposes the
    trace via ``ExpertEngine.last_trace`` so the ``/trace`` slash command
    can render it.
    """

    entries: list[TraceEntry] = field(default_factory=list)

    def record(self, **kwargs: Any) -> None:
        """Append a :class:`TraceEntry`."""
        self.entries.append(TraceEntry(**kwargs))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Missing:
    """Sentinel for "field not present" in dotted lookups."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING = _Missing()


def _walk(data: dict[str, Any], path: str) -> Any:
    """Walk a dotted path through nested dicts.

    Args:
        data: Starting dict.
        path: Dotted path like ``"user.id"`` or single key ``"name"``.

    Returns:
        The leaf value, or :data:`_MISSING` if any step is absent.
    """
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _matches_regex(actual: Any, pattern: Any) -> bool:
    """Apply :data:`PredicateOp.MATCHES` semantics — regex search on a string."""
    if not isinstance(actual, str) or not isinstance(pattern, str):
        return False
    try:
        return re.search(pattern, actual) is not None
    except re.error:
        return False


def _contains(haystack: Any, needle: Any) -> bool:
    """Substring search for strings, ``in`` for collections."""
    if haystack is None:
        return False
    try:
        return needle in haystack
    except TypeError:
        return False


_COMPARE: dict[PredicateOp, Any] = {
    PredicateOp.EQ: lambda a, b: a == b,
    PredicateOp.NE: lambda a, b: a != b,
    PredicateOp.IN: lambda a, b: a in (b or ()),
    PredicateOp.NOT_IN: lambda a, b: a not in (b or ()),
    PredicateOp.GT: lambda a, b: _safe_cmp(a, b, lambda x, y: x > y),
    PredicateOp.GTE: lambda a, b: _safe_cmp(a, b, lambda x, y: x >= y),
    PredicateOp.LT: lambda a, b: _safe_cmp(a, b, lambda x, y: x < y),
    PredicateOp.LTE: lambda a, b: _safe_cmp(a, b, lambda x, y: x <= y),
    PredicateOp.MATCHES: _matches_regex,
    PredicateOp.CONTAINS: _contains,
}


def _safe_cmp(a: Any, b: Any, op: Any) -> bool:
    """Numeric comparison that returns False rather than raising on mixed types."""
    try:
        return bool(op(a, b))
    except TypeError:
        return False
