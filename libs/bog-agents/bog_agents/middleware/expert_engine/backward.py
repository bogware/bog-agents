"""Backward chainer — proof-tree walker for the expert rule engine.

Forward chaining answers *"given these facts, what fires?"*. Backward
chaining answers two complementary questions:

* ``/why <pattern>`` — *"if a fact / activation matching this pattern
  exists, which rules could have produced it?"*. The walker locates every
  rule whose action consequents could yield such a fact (for
  ``assert_fact`` actions) or every activation that fired against this
  fact (for the immediately-recorded trace).
* ``/prove <goal>`` — *"could the engine derive this goal from current
  memory?"*. The walker treats the goal as a target fact, finds rules
  whose ``assert_fact`` actions could produce it, then recursively proves
  the antecedent patterns of those rules.

Both modes return a :class:`ProofTree` of :class:`ProofNode` objects so
the CLI can render them in any style (plain text, JSON, Rich tree).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bog_agents.middleware.expert_engine.matcher import PatternMatcher
from bog_agents.middleware.expert_engine.types import (
    Action,
    ActionKind,
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)
from bog_agents.middleware.expert_engine.working_memory import WorkingMemory

# ---------------------------------------------------------------------------
# Proof tree
# ---------------------------------------------------------------------------


@dataclass
class ProofNode:
    """A node in a proof tree.

    Attributes:
        label: Short text — e.g. ``"rule: prod_force_push_gate"`` or
            ``"fact: tool_call#42"`` or ``"unprovable: no rule asserts type X"``.
        kind: ``"rule"`` | ``"fact"`` | ``"unprovable"`` | ``"goal"``.
        proven: True iff the subtree under this node is satisfied.
        children: Sub-nodes (rule antecedents, supporting facts, etc.).
        metadata: Free-form payload for the renderer.
    """

    label: str
    kind: str = "rule"
    proven: bool = False
    children: list[ProofNode] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict (for the CLI / web UIs)."""
        return {
            "label": self.label,
            "kind": self.kind,
            "proven": self.proven,
            "metadata": self.metadata,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class ProofTree:
    """Wrapper around the root :class:`ProofNode` plus a flat list of all rules
    visited (handy for printing a summary).

    Attributes:
        root: The root node.
        rules_visited: Rule names traversed during the walk (deduped).
    """

    root: ProofNode
    rules_visited: list[str] = field(default_factory=list)

    @property
    def proven(self) -> bool:
        return self.root.proven

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root.to_dict(),
            "rules_visited": list(self.rules_visited),
        }


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


_MAX_DEPTH = 16


class BackwardChainer:
    """Walks rule consequents and conditions to answer *why* / *prove*.

    Args:
        rules: The rulebook to walk.
        memory: The working memory to test antecedent patterns against.
        max_depth: Hard recursion cap. Prevents pathological loops if a
            rule asserts a fact that activates the same rule. Default 16.
    """

    def __init__(
        self,
        rules: list[Rule],
        memory: WorkingMemory,
        *,
        max_depth: int = _MAX_DEPTH,
    ) -> None:
        self._rules = rules
        self._memory = memory
        self._matcher = PatternMatcher()
        self._max_depth = max(1, int(max_depth))

    # ------------------------------------------------------------------
    # /why <pattern>
    # ------------------------------------------------------------------

    def why(self, pattern: Pattern) -> ProofTree:
        """Explain how a fact matching *pattern* could have been produced.

        The walker finds every rule whose ``then`` includes an
        :class:`ActionKind.ASSERT_FACT` action of the matching fact type
        and tests its antecedent patterns against current memory.

        Args:
            pattern: The fact-shape to explain.

        Returns:
            A :class:`ProofTree`. ``proven`` is True iff at least one
            producer rule's antecedents currently hold.
        """
        root = ProofNode(
            label=f"why fact_type={pattern.fact_type}",
            kind="goal",
            metadata={"pattern": _pattern_summary(pattern)},
        )
        visited: list[str] = []

        # Direct evidence first — a present fact is its own proof.
        direct = [f for f in self._memory.by_type(pattern.fact_type) if pattern.matches_fact(f)]
        for fact in direct:
            root.children.append(
                ProofNode(
                    label=f"fact: {pattern.fact_type}#{fact.id}",
                    kind="fact",
                    proven=True,
                    metadata={"data": fact.data},
                )
            )

        producers = self._find_producers(pattern.fact_type)
        if not producers and not direct:
            root.children.append(
                ProofNode(
                    label=f"no rule asserts {pattern.fact_type}",
                    kind="unprovable",
                    proven=False,
                )
            )

        for rule in producers:
            node = self._explain_rule(rule, set(), 0)
            visited.append(rule.name)
            root.children.append(node)

        root.proven = any(c.proven for c in root.children)
        return ProofTree(root=root, rules_visited=_dedupe(visited))

    # ------------------------------------------------------------------
    # /prove <goal>
    # ------------------------------------------------------------------

    def prove(self, goal: Pattern) -> ProofTree:
        """Try to derive *goal* from current memory.

        First checks whether a matching fact already exists; if so, the
        goal is trivially proven. Otherwise recursively asks whether any
        producer rule's antecedents can be proven.
        """
        root = ProofNode(
            label=f"prove {goal.fact_type}",
            kind="goal",
            metadata={"pattern": _pattern_summary(goal)},
        )
        visited: list[str] = []
        # Direct evidence first.
        direct = [f for f in self._memory.by_type(goal.fact_type) if goal.matches_fact(f)]
        if direct:
            for fact in direct:
                root.children.append(
                    ProofNode(
                        label=f"already-asserted fact: {goal.fact_type}#{fact.id}",
                        kind="fact",
                        proven=True,
                        metadata={"data": fact.data},
                    )
                )
            root.proven = True
            return ProofTree(root=root, rules_visited=visited)

        # Recursive derivation.
        producers = self._find_producers(goal.fact_type)
        for rule in producers:
            node = self._prove_rule(rule, set(), 0)
            visited.append(rule.name)
            root.children.append(node)
        if not producers:
            root.children.append(
                ProofNode(
                    label=f"no rule asserts {goal.fact_type}",
                    kind="unprovable",
                    proven=False,
                )
            )
        root.proven = any(c.proven for c in root.children)
        return ProofTree(root=root, rules_visited=_dedupe(visited))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_producers(self, fact_type: str) -> list[Rule]:
        """Return rules whose ``then`` includes an ``assert_fact`` of this type."""
        producers: list[Rule] = []
        for rule in self._rules:
            for action in rule.then:
                if action.kind is ActionKind.ASSERT_FACT and action.params.get("fact_type") == fact_type:
                    producers.append(rule)
                    break
        return producers

    def _explain_rule(
        self,
        rule: Rule,
        seen: set[str],
        depth: int,
    ) -> ProofNode:
        """Build the proof subtree for one producer rule (explanatory mode)."""
        if rule.name in seen or depth >= self._max_depth:
            return ProofNode(
                label=f"rule: {rule.name} (cycle / depth limit)",
                kind="rule",
                proven=False,
            )
        seen = seen | {rule.name}
        node = ProofNode(
            label=f"rule: {rule.name}",
            kind="rule",
            metadata={"description": rule.description, "salience": rule.salience},
        )
        all_match = True
        if not rule.when:
            node.children.append(
                ProofNode(
                    label="(no antecedents — fires unconditionally)",
                    kind="fact",
                    proven=True,
                )
            )
        for pattern in rule.when:
            child = self._explain_pattern(pattern, seen, depth + 1)
            node.children.append(child)
            if not child.proven:
                all_match = False
        node.proven = all_match
        return node

    def _explain_pattern(
        self,
        pattern: Pattern,
        seen: set[str],
        depth: int,
    ) -> ProofNode:
        """Build the proof subtree for one antecedent pattern."""
        # Check for a directly-matching fact in memory first.
        matches = [f for f in self._memory.by_type(pattern.fact_type) if pattern.matches_fact(f)]
        if matches:
            head = matches[0]
            return ProofNode(
                label=f"pattern: {pattern.fact_type} (matched #{head.id})",
                kind="fact",
                proven=not pattern.negated,
                metadata={"matched_id": head.id, "data": head.data},
            )
        # No direct match — descend into producers.
        producers = self._find_producers(pattern.fact_type)
        node = ProofNode(
            label=f"pattern: {pattern.fact_type} (no direct match)",
            kind="rule",
            proven=pattern.negated,  # negation as failure: no match = proven negation
        )
        for rule in producers:
            sub = self._explain_rule(rule, seen, depth + 1)
            node.children.append(sub)
            if sub.proven and not pattern.negated:
                node.proven = True
        return node

    def _prove_rule(
        self,
        rule: Rule,
        seen: set[str],
        depth: int,
    ) -> ProofNode:
        """Same as ``_explain_rule`` but tagged for the prove flow."""
        node = self._explain_rule(rule, seen, depth)
        node.label = node.label.replace("rule:", "via rule:", 1)
        return node


# ---------------------------------------------------------------------------
# Pretty helpers
# ---------------------------------------------------------------------------


def _pattern_summary(pattern: Pattern) -> dict[str, Any]:
    """Compact JSON-friendly summary of a pattern (for tree metadata)."""
    return {
        "fact_type": pattern.fact_type,
        "negated": pattern.negated,
        "bind": pattern.bind,
        "predicates": [_predicate_summary(p) for p in pattern.predicates],
    }


def _predicate_summary(pred: Predicate) -> dict[str, Any]:
    return {"field": pred.field, "op": pred.op.value, "value": pred.value}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Quick-look helpers used by the CLI ``/why`` and ``/prove`` commands
# ---------------------------------------------------------------------------


def render_tree(tree: ProofTree, *, indent: str = "  ") -> str:
    """Render a :class:`ProofTree` as plain text for the ``/why`` slash output."""
    lines: list[str] = []
    _render_node(tree.root, indent, 0, lines)
    if tree.rules_visited:
        lines.append("")
        lines.append(f"rules visited: {', '.join(tree.rules_visited)}")
    return "\n".join(lines)


def _render_node(node: ProofNode, indent: str, depth: int, out: list[str]) -> None:
    mark = "✓" if node.proven else "✗"
    out.append(f"{indent * depth}{mark} {node.label}")
    for child in node.children:
        _render_node(child, indent, depth + 1, out)


# Convenience: build a Pattern from a (fact_type, field=value, …) shorthand,
# used by the CLI parser layer.


def pattern_from_shorthand(fact_type: str, **fields: Any) -> Pattern:
    """Build a simple equality pattern from keyword args.

    Example::

        pattern_from_shorthand("tool_call", name="shell_execute", session_id=7)
    """
    preds = tuple(Predicate(field=k, op=PredicateOp.EQ, value=v) for k, v in fields.items())
    return Pattern(fact_type=fact_type, predicates=preds)


# Allow ActionExecutor to be imported without pulling backward.py in.
__all__ = [
    "Action",  # re-export for users assembling rules in code
    "BackwardChainer",
    "Fact",
    "ProofNode",
    "ProofTree",
    "pattern_from_shorthand",
    "render_tree",
]
