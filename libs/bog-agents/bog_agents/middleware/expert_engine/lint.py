"""Static analysis for expert-rule rulebooks.

``lint(rules)`` walks a loaded ``list[Rule]`` and flags common mistakes
without ever running the engine:

* **duplicate-name** — two rules with the same ``name``. The engine
  doesn't error on this but conflict-resolution becomes order-dependent.
* **dead-rule** — a rule whose ``when`` patterns reference a fact_type
  that no other rule's ``then`` ever asserts AND that the engine never
  receives from outside (e.g. ``tool_call``, ``session``, ``context``,
  ``file_edit``). The default engine assertion set is hard-coded here so
  authors get the right hint when the typo is on their side.
* **conflicting-actions** — two rules that match the same fact-type
  on overlapping predicate fields and emit contradictory actions
  (one ``deny`` and one ``modify`` / ``require_approval`` on the same
  pattern). The check is a conservative overlap heuristic, not a
  theorem-prover.
* **redundant-predicate** — a pattern that asserts the same equality
  twice on the same field (e.g. ``name: shell`` then ``name: shell``).
* **always-fires** — a rule with no ``when`` patterns (fires once per
  run regardless of facts). Usually intentional but worth flagging.
* **no-actions** — a rule whose ``then`` is empty. Almost certainly an
  authoring mistake.

Designed for the ``/expert lint`` slash command. Returns a list of
:class:`LintFinding` so the CLI can render them; the engine itself does
not depend on this module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bog_agents.middleware.expert_engine.types import (
    ActionKind,
)

if TYPE_CHECKING:
    from bog_agents.middleware.expert_engine.types import Rule

# Fact types the engine itself injects (or that downstream middleware
# routinely assert). A rule that matches one of these is NOT dead even
# if no in-rulebook ``assert_fact`` produces it.
_ENGINE_FACT_TYPES: frozenset[str] = frozenset(
    {
        "tool_call",
        "session",
        "context",
        "file_edit",
        "git_event",
        "user_message",
        "model_response",
    }
)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LintFinding:
    """One lint diagnostic.

    Attributes:
        severity: ``"error"`` | ``"warning"`` | ``"info"``.
        code: Short kebab-case identifier; matches the bullet list in the
            module docstring (``"dead-rule"`` etc.).
        rule_name: Name of the offending rule (or empty for whole-file
            findings).
        message: Human-readable description.
    """

    severity: str
    code: str
    rule_name: str
    message: str


@dataclass
class LintReport:
    """Aggregate result of a :func:`lint` call."""

    findings: list[LintFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def infos(self) -> list[LintFinding]:
        return [f for f in self.findings if f.severity == "info"]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.warnings

    def add(
        self,
        severity: str,
        code: str,
        rule_name: str,
        message: str,
    ) -> None:
        self.findings.append(LintFinding(severity=severity, code=code, rule_name=rule_name, message=message))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def lint(rules: list[Rule]) -> LintReport:
    """Run every check against *rules* and return the aggregate report."""
    report = LintReport()
    _check_duplicate_names(rules, report)
    for rule in rules:
        _check_no_actions(rule, report)
        _check_always_fires(rule, report)
        _check_redundant_predicates(rule, report)
    _check_dead_rules(rules, report)
    _check_conflicting_actions(rules, report)
    return report


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_duplicate_names(rules: list[Rule], report: LintReport) -> None:
    counts = Counter(r.name for r in rules)
    for name, n in counts.items():
        if n > 1:
            report.add(
                severity="error",
                code="duplicate-name",
                rule_name=name,
                message=(f"rule name {name!r} is used by {n} rules — conflict resolution depends on file/load order which is fragile"),
            )


def _check_no_actions(rule: Rule, report: LintReport) -> None:
    if not rule.then:
        report.add(
            severity="warning",
            code="no-actions",
            rule_name=rule.name,
            message=("rule has no ``then`` actions — matching costs CPU but has no observable effect. Probably a typo; remove or add an action."),
        )


def _check_always_fires(rule: Rule, report: LintReport) -> None:
    if not rule.when:
        report.add(
            severity="info",
            code="always-fires",
            rule_name=rule.name,
            message=(
                "rule has no ``when`` patterns; it fires exactly once per "
                "engine run. Intended for bootstrap rules; double-check this "
                "is what you meant."
            ),
        )


def _check_redundant_predicates(rule: Rule, report: LintReport) -> None:
    for pat in rule.when:
        seen: dict[tuple[str, str], object] = {}
        for pred in pat.predicates:
            key = (pred.field, pred.op.value)
            if key in seen and seen[key] != pred.value:
                report.add(
                    severity="warning",
                    code="redundant-predicate",
                    rule_name=rule.name,
                    message=(
                        f"pattern on {pat.fact_type!r} has two ``{pred.field}.{pred.op.value}`` "
                        f"checks with different values ({seen[key]!r} vs {pred.value!r}); "
                        "second one wins, first is dead"
                    ),
                )
            seen[key] = pred.value


def _check_dead_rules(rules: list[Rule], report: LintReport) -> None:
    # Fact types that any rule's ``then`` can produce.
    produced: set[str] = set()
    for r in rules:
        for action in r.then:
            if action.kind is ActionKind.ASSERT_FACT:
                ft = action.params.get("fact_type")
                if isinstance(ft, str):
                    produced.add(ft)
    reachable = _ENGINE_FACT_TYPES | produced
    for rule in rules:
        for pat in rule.when:
            if pat.negated:
                continue
            if pat.fact_type in reachable:
                continue
            report.add(
                severity="warning",
                code="dead-rule",
                rule_name=rule.name,
                message=(
                    f"rule references fact_type {pat.fact_type!r} but no other rule "
                    "asserts it AND it's not a default-engine-asserted type "
                    f"({sorted(_ENGINE_FACT_TYPES)}). Likely a typo."
                ),
            )
            break  # one warning per rule is enough


def _check_conflicting_actions(rules: list[Rule], report: LintReport) -> None:
    """Conservative overlap heuristic.

    Group rules by the set of fact_types they match. Within each group,
    if at least one rule emits ``deny`` and at least one other emits a
    non-deny action, warn — the resolution is salience-driven, which the
    author may not have considered.
    """
    by_fact_types: dict[frozenset[str], list[Rule]] = {}
    for r in rules:
        if not r.when:
            continue
        key = frozenset(p.fact_type for p in r.when if not p.negated)
        if not key:
            continue
        by_fact_types.setdefault(key, []).append(r)
    for fact_types, rule_group in by_fact_types.items():
        if len(rule_group) < 2:
            continue
        deniers = [r for r in rule_group if _has_action(r, ActionKind.DENY)]
        modifiers = [r for r in rule_group if _has_action(r, ActionKind.MODIFY)]
        if deniers and modifiers:
            names = sorted({*[r.name for r in deniers], *[r.name for r in modifiers]})
            report.add(
                severity="info",
                code="conflicting-actions",
                rule_name="",
                message=(
                    f"rules {names!r} all match on the same fact-type set "
                    f"{sorted(fact_types)!r} and emit a mix of deny + modify. "
                    "Salience decides which wins; confirm the order is "
                    "intentional with explicit ``salience:`` values."
                ),
            )


def _has_action(rule: Rule, kind: ActionKind) -> bool:
    return any(a.kind is kind for a in rule.then)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(report: LintReport) -> str:
    """Render *report* as plain text for the ``/expert lint`` slash output."""
    if not report.findings:
        return "Lint: no issues found."
    lines = [f"Lint: {len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.infos)} info"]
    for f in report.findings:
        prefix = {"error": "✗", "warning": "⚠", "info": "ℹ"}.get(f.severity, "•")  # noqa: RUF001
        scope = f" {f.rule_name}" if f.rule_name else ""
        lines.append(f"  {prefix} [{f.code}]{scope}: {f.message}")
    return "\n".join(lines)


__all__ = ["LintFinding", "LintReport", "lint", "render_report"]
