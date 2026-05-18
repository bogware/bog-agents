"""Pattern matcher for the expert rule engine.

Matches a tuple of :class:`Pattern` against the :class:`WorkingMemory`,
producing zero or more :class:`Match` objects. Each :class:`Match` carries
the variable bindings and the matched facts in pattern order.

The algorithm is a depth-first join: for each pattern in order, scan the
candidate facts of that pattern's type, keep only those whose predicates
hold, and (when the pattern is bound) propagate the binding to subsequent
patterns. Negated patterns succeed only when no candidate fact matches.

Predicate values may reference earlier bindings via the ``{{var}}`` syntax.
This is what makes multi-pattern joins useful — e.g. *"a tool_call whose
session matches the open session"* is a two-pattern rule with one binding.

Complexity: O(P · F^k) worst-case, where P is rules, F is facts per type,
and k is the maximum number of patterns per rule. For realistic policy
rulebooks (< 100 rules, < 1k facts, < 5 patterns/rule) this completes in
microseconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from bog_agents.middleware.expert_engine.types import (
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
)
from bog_agents.middleware.expert_engine.working_memory import WorkingMemory


@dataclass(frozen=True)
class Match:
    """A complete match across all of a rule's patterns.

    Attributes:
        bindings: Variable name → matched fact, populated for each pattern
            that declared a ``bind``.
        matched_facts: Facts in pattern order. Negated patterns contribute
            no fact and are skipped in this tuple.
    """

    bindings: dict[str, Fact] = field(default_factory=dict, hash=False, compare=False)
    matched_facts: tuple[Fact, ...] = ()


_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")


class PatternMatcher:
    """Match rule patterns against :class:`WorkingMemory`.

    The matcher is stateless — construct one and call :meth:`match_all` per
    rule. Cheap to create.
    """

    def match_all(
        self,
        patterns: tuple[Pattern, ...],
        memory: WorkingMemory,
    ) -> list[Match]:
        """Return every :class:`Match` for *patterns* in *memory*.

        Args:
            patterns: Patterns in rule-declared order.
            memory: Working memory to match against.

        Returns:
            A list of matches. A rule with no patterns produces one empty
            match (so empty-``when`` rules fire once per run).
        """
        if not patterns:
            return [Match()]
        return list(self._join(list(patterns), 0, Match(), memory))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _join(
        self,
        patterns: list[Pattern],
        idx: int,
        partial: Match,
        memory: WorkingMemory,
    ) -> list[Match]:
        """Depth-first join across the remaining patterns."""
        if idx >= len(patterns):
            return [partial]
        pat = patterns[idx]
        results: list[Match] = []
        if pat.negated:
            # Negation as failure: succeed iff no fact of this type matches.
            for candidate in memory.by_type(pat.fact_type):
                if self._fact_matches(pat, candidate, partial.bindings):
                    return []  # at least one match → negation fails
            results.extend(self._join(patterns, idx + 1, partial, memory))
            return results

        for candidate in memory.by_type(pat.fact_type):
            if not self._fact_matches(pat, candidate, partial.bindings):
                continue
            new_bindings = dict(partial.bindings)
            if pat.bind:
                new_bindings[pat.bind] = candidate
            new_facts = (*partial.matched_facts, candidate)
            next_partial = Match(bindings=new_bindings, matched_facts=new_facts)
            results.extend(self._join(patterns, idx + 1, next_partial, memory))
        return results

    def _fact_matches(
        self,
        pattern: Pattern,
        fact: Fact,
        bindings: dict[str, Fact],
    ) -> bool:
        """Apply every predicate (with binding resolution) to *fact*."""
        if fact.fact_type != pattern.fact_type:
            return False
        for pred in pattern.predicates:
            resolved = _resolve_predicate(pred, bindings)
            if not resolved.test(fact):
                return False
        return True


# ---------------------------------------------------------------------------
# Template / binding resolution
# ---------------------------------------------------------------------------


def _resolve_predicate(pred: Predicate, bindings: dict[str, Fact]) -> Predicate:
    """Resolve ``{{var}}`` templates in a predicate's value.

    Args:
        pred: The original predicate.
        bindings: Variable name → :class:`Fact`. ``{{var}}`` resolves to
            the entire fact. ``{{var.field}}`` walks the fact's data.

    Returns:
        A new :class:`Predicate` with templates substituted, or the
        original predicate if it has no templates.
    """
    if pred.op in (PredicateOp.EXISTS, PredicateOp.MISSING):
        return pred
    return Predicate(
        field=pred.field,
        op=pred.op,
        value=resolve_value(pred.value, bindings),
    )


def resolve_value(value: Any, bindings: dict[str, Fact]) -> Any:
    """Substitute ``{{var}}`` templates in a value.

    Supports:

    * Strings — every ``{{...}}`` is replaced. If the whole string is a
      single template the original-typed value is returned (so ``{{c.cost}}``
      keeps its float type rather than becoming ``"3.14"``).
    * Lists — each element is resolved.
    * Dicts — values are resolved; keys are left alone.
    * Anything else — returned as-is.

    Unknown variables resolve to the literal ``{{var}}`` string so the
    matcher can still test (and fail) without raising.
    """
    if isinstance(value, str):
        return _resolve_string(value, bindings)
    if isinstance(value, list):
        return [resolve_value(v, bindings) for v in value]
    if isinstance(value, dict):
        return {k: resolve_value(v, bindings) for k, v in value.items()}
    return value


def _resolve_string(text: str, bindings: dict[str, Fact]) -> Any:
    """Apply ``{{var}}`` templating to *text*."""
    if "{{" not in text:
        return text

    # Special case: whole string is exactly one template — preserve type.
    stripped = text.strip()
    sole = _TEMPLATE_RE.fullmatch(stripped)
    if sole is not None:
        resolved = _lookup(sole.group(1), bindings)
        if resolved is not _UNRESOLVED:
            return resolved
        return text

    def replace(m: re.Match[str]) -> str:
        val = _lookup(m.group(1), bindings)
        if val is _UNRESOLVED:
            return m.group(0)
        return str(val)

    return _TEMPLATE_RE.sub(replace, text)


class _Unresolved:
    __slots__ = ()


_UNRESOLVED = _Unresolved()


def _lookup(path: str, bindings: dict[str, Fact]) -> Any:
    """Walk ``var.field.subfield`` against the bindings."""
    parts = path.split(".")
    head, rest = parts[0], parts[1:]
    fact = bindings.get(head)
    if fact is None:
        return _UNRESOLVED
    if not rest:
        return fact
    cur: Any = fact.data
    for part in rest:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _UNRESOLVED
    return cur
