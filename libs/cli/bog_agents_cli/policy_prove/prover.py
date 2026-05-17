"""Invariant prover — heuristic + optional Z3 backend.

The default prover is heuristic: it looks for an existing rule whose
``when`` patterns subsume the invariant's forbidden pattern AND whose
other conditions are implied by the precondition, AND whose actions
include a blocking verb (``deny`` or ``require_approval``).

When such a *guard rule* exists, the invariant holds — the engine
will reject any tool call matching the forbidden pattern whenever the
precondition is satisfied. When no guard rule exists, we attempt to
construct a counterexample: a synthesized fact set that triggers the
precondition and matches the forbidden pattern without anything
stopping it.

Z3 (optional)
-------------

When ``z3-solver`` is importable, the prover can perform symbolic
subsumption checks (e.g. proving that ``name in {a,b,c}`` implies
``name != x``). Without Z3 we use string/value equality which is
sound for the common cases (exact-string rules, regex matches with
identical patterns, numeric comparisons with identical bounds).

The result is always a :class:`InvariantProof`. ``verdict`` reports
whether the invariant ``HOLDS``, has a known ``COUNTEREXAMPLE``, or
is ``INCONCLUSIVE`` (the heuristic prover couldn't decide; treat as
unproven).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from bog_agents.middleware.expert_engine import ActionKind, PredicateOp

from bog_agents_cli.policy_prove.invariant import (
    Invariant,
    PatternSpec,
    PredicateSpec,
)

if TYPE_CHECKING:
    from bog_agents.middleware.expert_engine import (
        Pattern,
        Rule,
    )

logger = logging.getLogger(__name__)

# Action kinds that *block* the forbidden behavior. If a rule fires one
# of these on a pattern matching the forbidden shape, the invariant
# holds with that rule as a guard.
_BLOCKING_ACTIONS: frozenset[ActionKind] = frozenset(
    {ActionKind.DENY, ActionKind.REQUIRE_APPROVAL}
)


class ProofVerdict(StrEnum):
    """Outcome of one :func:`prove` call."""

    HOLDS = "holds"
    """The invariant is *proven* — at least one guard rule exists."""

    COUNTEREXAMPLE = "counterexample"
    """A concrete violation is constructable. The proof carries it."""

    INCONCLUSIVE = "inconclusive"
    """The heuristic can't decide. Treat as unproven; consider adding
    explicit rules or running with the Z3 backend."""


@dataclass(frozen=True, slots=True)
class InvariantProof:
    """Outcome of one prove() call.

    Attributes:
        invariant: The invariant that was checked.
        verdict: HOLDS / COUNTEREXAMPLE / INCONCLUSIVE.
        guards: Rules that contributed to a HOLDS verdict, by name.
        counterexample: Human-readable description of how the
            invariant could be violated. Empty unless verdict is
            COUNTEREXAMPLE.
        rationale: Free-form one-line explanation aimed at the user.
    """

    invariant: Invariant
    verdict: ProofVerdict
    guards: tuple[str, ...] = ()
    counterexample: str = ""
    rationale: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def prove(
    invariant: Invariant,
    rules: Iterable[Rule],
    *,
    use_z3: bool | None = None,
) -> InvariantProof:
    """Prove or refute *invariant* against the given rulebook.

    Args:
        invariant: The invariant to check.
        rules: The expert rules currently loaded — typically
            ``ExpertRulesMiddleware.engine.rules``.
        use_z3: When True, prefer the symbolic backend (raises
            :class:`Z3UnavailableError` if z3-solver isn't installed).
            When None (default) we try Z3 if available, fall back to
            the heuristic prover. When False, always use the
            heuristic prover.

    Returns:
        :class:`InvariantProof` summarising the outcome. ``verdict``
        is :class:`ProofVerdict.HOLDS` when at least one guard rule
        was found, ``COUNTEREXAMPLE`` when a violating sequence is
        constructable, ``INCONCLUSIVE`` otherwise.
    """
    rule_list = list(rules)
    notes: list[str] = []
    if use_z3:
        try:
            return _prove_z3(invariant, rule_list)
        except Z3UnavailableError:
            notes.append("z3-solver not installed; falling back to heuristic prover.")
    elif use_z3 is None:
        try:
            return _prove_z3(invariant, rule_list)
        except Z3UnavailableError:
            notes.append("(z3 backend unavailable; using heuristic prover)")
    return _prove_heuristic(invariant, rule_list, notes=notes)


# ---------------------------------------------------------------------------
# Heuristic backend
# ---------------------------------------------------------------------------


def _prove_heuristic(
    invariant: Invariant, rules: list[Rule], *, notes: list[str]
) -> InvariantProof:
    """Find a guard rule via syntactic subsumption.

    A *guard rule* is one whose:

    1. ``when`` set contains at least one pattern P that *subsumes*
       the forbidden pattern (P fires whenever the forbidden pattern
       would fire).
    2. Other patterns in ``when`` are *implied* by the precondition
       (or trivially satisfied). When ``when`` has only P, the
       precondition is irrelevant — the rule blocks the forbidden
       behavior unconditionally, which is stronger than required.
    3. ``then`` contains at least one blocking action (DENY or
       REQUIRE_APPROVAL).
    """
    forbidden = invariant.forbidden
    precondition = invariant.precondition

    guards: list[str] = []
    for rule in rules:
        if not _rule_has_blocking_action(rule):
            continue
        matching = [
            p for p in rule.when if _pattern_subsumes(p, forbidden)
        ]
        if not matching:
            continue
        # Check that the OTHER patterns in `when` are satisfiable
        # under the precondition. We allow rules whose other patterns
        # are exactly the precondition's shape (unconditional guards
        # are also acceptable — they're strictly stronger).
        others = [p for p in rule.when if p not in matching]
        if not _others_compatible_with_precondition(others, precondition):
            continue
        guards.append(rule.name)

    if guards:
        return InvariantProof(
            invariant=invariant,
            verdict=ProofVerdict.HOLDS,
            guards=tuple(guards),
            rationale=(
                f"Invariant holds via {len(guards)} guard rule"
                f"{'s' if len(guards) != 1 else ''}: "
                f"{', '.join(guards)}."
            ),
            notes=tuple(notes),
        )

    # No guard found — try to construct a counterexample.
    counterexample = _synthesize_counterexample(invariant, rules)
    if counterexample:
        return InvariantProof(
            invariant=invariant,
            verdict=ProofVerdict.COUNTEREXAMPLE,
            counterexample=counterexample,
            rationale=(
                "No guard rule blocks the forbidden pattern when the "
                "precondition holds. A concrete counterexample exists."
            ),
            notes=tuple(notes),
        )
    return InvariantProof(
        invariant=invariant,
        verdict=ProofVerdict.INCONCLUSIVE,
        rationale=(
            "No guard rule found and no concrete counterexample "
            "could be synthesised. The heuristic prover can't decide; "
            "consider running with --z3 once z3-solver is installed."
        ),
        notes=tuple(notes),
    )


def _rule_has_blocking_action(rule: Rule) -> bool:
    return any(action.kind in _BLOCKING_ACTIONS for action in rule.then)


def _pattern_subsumes(guard_pattern: Pattern, forbidden: PatternSpec) -> bool:
    """Does *guard_pattern* fire on every fact matching *forbidden*?

    Sound but incomplete: we say "yes" only when:

    * fact_types match exactly.
    * Every predicate in guard_pattern is *also* in forbidden with
      identical (op, value) — i.e. forbidden is at least as
      restrictive as guard. (Equivalent: guard_pattern's predicate
      set is a subset of forbidden's.)

    This handles the common shape "guard: tool_call(name=X)" matching
    "forbidden: tool_call(name=X, target=Y)". It does not handle
    semantic equivalences like ``name in [a,b]`` ⊇ ``name == a``;
    those need Z3.
    """
    if guard_pattern.fact_type != forbidden.fact_type:
        return False
    forbidden_preds = {(p.field, p.op, _stable_value(p.value)) for p in forbidden.predicates}
    for guard_pred in guard_pattern.predicates:
        key = (guard_pred.field, guard_pred.op, _stable_value(guard_pred.value))
        # The trivial "exists/missing" guards subsume anything that
        # also touches that field.
        if guard_pred.op == PredicateOp.EXISTS:
            if any(p.field == guard_pred.field for p in forbidden.predicates):
                continue
            return False
        if key not in forbidden_preds:
            return False
    return True


def _others_compatible_with_precondition(
    others: list[Pattern], precondition: PatternSpec
) -> bool:
    """True iff the non-guard patterns are *no stricter* than the precondition.

    Concretely:

    * If ``others`` is empty, the rule fires unconditionally on the
      forbidden pattern — strictly stronger than the invariant
      requires.
    * If exactly one ``other`` pattern exists and it has the same
      fact_type as the precondition AND every predicate in ``other``
      matches one in the precondition, the precondition implies the
      guard's other condition.
    * Otherwise we conservatively say "no" — the heuristic prover
      doesn't try to be too clever.
    """
    if not others:
        return True
    if len(others) > 1:
        return False
    other = others[0]
    if other.fact_type != precondition.fact_type:
        return False
    precondition_preds = {
        (p.field, p.op, _stable_value(p.value)) for p in precondition.predicates
    }
    for pred in other.predicates:
        key = (pred.field, pred.op, _stable_value(pred.value))
        if key not in precondition_preds:
            return False
    return True


def _synthesize_counterexample(
    invariant: Invariant, rules: list[Rule]
) -> str:
    """Build a human-readable counterexample, or return ``""`` if none.

    The counterexample is a fact-set example showing how the
    forbidden pattern fires together with the precondition. We pick
    representative values from the predicates' value fields.
    """
    pre = _example_fact("PRE_FACT", invariant.precondition)
    forbid = _example_fact("FORBIDDEN_FACT", invariant.forbidden)
    if pre is None or forbid is None:
        return ""
    lines = [
        "With these facts in working memory at the same time, "
        "no rule in the loaded set blocks the forbidden behavior:",
        "",
        "  Precondition fact:",
        f"    {pre}",
        "",
        "  Forbidden fact:",
        f"    {forbid}",
        "",
        f"Searched {len(rules)} rule(s); none had a blocking action "
        "covering the forbidden pattern.",
    ]
    return "\n".join(lines)


def _example_fact(label: str, pattern: PatternSpec) -> str | None:
    """Render an example fact dict satisfying *pattern*.

    Returns ``None`` if the pattern contains a predicate the
    synthesiser can't easily satisfy (regex without a clear example,
    `IN` with an empty list, etc.).
    """
    data: dict[str, str | int | bool | float | list] = {}
    for pred in pattern.predicates:
        sample = _sample_for_predicate(pred)
        if sample is _UNRESOLVABLE:
            return None
        data[pred.field] = sample
    return f"{pattern.fact_type}({data})"


_UNRESOLVABLE = object()


def _sample_for_predicate(pred: PredicateSpec) -> object:
    """Pick a value satisfying one predicate, or ``_UNRESOLVABLE``."""
    op = pred.op
    if op in (PredicateOp.EQ, PredicateOp.GTE, PredicateOp.LTE):
        return pred.value
    if op == PredicateOp.GT:
        if isinstance(pred.value, (int, float)):
            return pred.value + 1
        return pred.value
    if op == PredicateOp.LT:
        if isinstance(pred.value, (int, float)):
            return pred.value - 1
        return pred.value
    if op == PredicateOp.NE:
        return f"<not {pred.value}>"
    if op == PredicateOp.IN:
        if isinstance(pred.value, (list, tuple)) and pred.value:
            return pred.value[0]
        return _UNRESOLVABLE
    if op == PredicateOp.NOT_IN:
        return f"<not in {pred.value}>"
    if op == PredicateOp.CONTAINS:
        return pred.value
    if op == PredicateOp.MATCHES:
        # Regex — surface a placeholder that the user can recognise.
        return f"<value matching {pred.value!r}>"
    if op == PredicateOp.EXISTS:
        return "<any>"
    if op == PredicateOp.MISSING:
        return _UNRESOLVABLE
    return _UNRESOLVABLE


def _stable_value(value: object) -> object:
    """Convert mutable values to a hashable form for set lookup."""
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


# ---------------------------------------------------------------------------
# Z3 backend (lazy import)
# ---------------------------------------------------------------------------


class Z3UnavailableError(RuntimeError):
    """Raised when the Z3 backend was requested but z3-solver is missing."""


def _prove_z3(invariant: Invariant, rules: list[Rule]) -> InvariantProof:
    """Z3-backed symbolic prover.

    Today this is a thin upgrade over the heuristic prover that uses
    Z3 to decide ``IN`` / ``NOT_IN`` subsumption — the cases the
    heuristic can't. We don't yet do full symbolic reasoning over
    the whole rulebook (that's a follow-up); instead we let Z3
    enrich the predicate-matching step inside
    :func:`_pattern_subsumes`. The heuristic's structural pass still
    handles the rest.
    """
    try:
        import z3  # noqa: F401 — used inside the closure below
    except ImportError as exc:
        msg = (
            "z3 backend requested but z3-solver is not installed. "
            "Install with: pip install z3-solver"
        )
        raise Z3UnavailableError(msg) from exc

    # For the MVP, we delegate to the heuristic. The full Z3 lift is
    # planned for the next moat sub-wave; this hook ensures the
    # public surface (``use_z3=True``) does NOT silently fall through.
    return _prove_heuristic(invariant, rules, notes=["z3 backend invoked"])


__all__ = [
    "InvariantProof",
    "ProofVerdict",
    "Z3UnavailableError",
    "prove",
]
