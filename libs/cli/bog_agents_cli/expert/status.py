"""``/expert`` status, list, show, trace, run, lint, memory, dry-run, example.

Free functions that take the :class:`ExpertController` as their first
argument. Method bodies are unchanged from the original
``expert_controller.py`` — the controller now delegates here so each
concern lives in a smaller file.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.middleware.expert_engine import (
    Fact,
    lint as lint_rules,
    render_report as render_lint_report,
)
from bog_agents.middleware.expert_engine.backward import render_tree

from bog_agents_cli.expert._helpers import _EXAMPLE_RULE, _pattern_from_kv

if TYPE_CHECKING:
    from bog_agents_cli.expert_controller import ExpertController


def status(controller: ExpertController) -> str:
    """Human-readable status — used by ``/expert`` with no subcommand."""
    mw = controller.middleware
    rule_count = len(mw.engine.rules)
    counters = mw.counters
    state = "ON" if mw.enabled else "OFF"
    rules_dir = controller._working_dir / ".bog-agents" / "expert_rules"
    lines = [
        f"Expert mode: {state}",
        f"Rules loaded: {rule_count} (from {rules_dir})",
        f"Denials: {counters['denials']}  "
        f"Modifications: {counters['modifications']}  "
        f"Approvals: {counters['approvals']}",
    ]
    if rule_count > 0:
        lines.append("")
        lines.append("Loaded rules (in declaration order):")
        for rule in mw.engine.rules:
            src = Path(rule.source_file).name if rule.source_file else "<programmatic>"
            desc = f" — {rule.description}" if rule.description else ""
            lines.append(
                f"  {rule.name}  [salience={rule.salience}]  ({src}){desc}"
            )
    else:
        lines.append("")
        lines.append(f"Create a rule file in {rules_dir} to get started.")
        lines.append("See /expert example for a template.")
    return "\n".join(lines)


def set_enabled(controller: ExpertController, on: bool) -> str:
    """Toggle the engine on/off."""
    controller.middleware.set_enabled(on)
    return f"Expert mode: {'ON' if on else 'OFF'}"


def reload(controller: ExpertController) -> str:
    """Force a rule reload from disk."""
    count, err = controller.middleware.reload()
    if err:
        return f"Reload completed with errors:\n  {err}\n\n(kept the previous {count} rules live)"
    return f"Reloaded {count} rule(s) from disk."


def list_rules(controller: ExpertController) -> str:
    """List every loaded rule's name + summary."""
    rules = controller.middleware.engine.rules
    if not rules:
        return "No rules loaded. Drop a YAML file into .bog-agents/expert_rules/."
    lines = [f"{len(rules)} rule(s) loaded:"]
    for rule in rules:
        src = Path(rule.source_file).name if rule.source_file else "<programmatic>"
        desc = f" — {rule.description}" if rule.description else ""
        lines.append(
            f"  • {rule.name}  [salience={rule.salience}, once={rule.once}]  ({src}){desc}"
        )
    return "\n".join(lines)


def show_rule(controller: ExpertController, name: str) -> str:
    """Show one rule's source file path + summary."""
    if not name:
        return "Usage: /expert show <rule-name>"
    match = next(
        (r for r in controller.middleware.engine.rules if r.name == name),
        None,
    )
    if match is None:
        return f"Rule '{name}' not found. Use /expert list."
    lines = [
        f"Rule: {match.name}",
        f"Source: {match.source_file or '<programmatic>'}",
        f"Salience: {match.salience}  Once: {match.once}",
    ]
    if match.description:
        lines.append(f"Description: {match.description}")
    lines.append("")
    lines.append(f"When ({len(match.when)} pattern(s)):")
    for pat in match.when:
        preds = ", ".join(
            f"{p.field}.{p.op.value}={p.value!r}" for p in pat.predicates
        ) or "(no predicates)"
        neg = " NOT" if pat.negated else ""
        bind = f" $bind={pat.bind}" if pat.bind else ""
        lines.append(f"  -{neg} {pat.fact_type}({preds}){bind}")
    lines.append("")
    lines.append(f"Then ({len(match.then)} action(s)):")
    for act in match.then:
        params = ", ".join(f"{k}={v!r}" for k, v in act.params.items())
        lines.append(f"  - {act.kind.value}({params})")
    return "\n".join(lines)


def trace(controller: ExpertController, limit: int = 50) -> str:
    """Render the last engine run trace (up to *limit* entries)."""
    entries = controller.middleware.last_trace()
    if not entries:
        return "No trace available — no tool call has run through the engine yet."
    lines = [f"Last engine trace ({len(entries)} entries):"]
    for e in entries[-limit:]:
        stamp = f"[{e['kind']}]"
        rule = f" {e['rule']}" if e["rule"] else ""
        detail = f" — {e['detail']}" if e["detail"] else ""
        lines.append(f"  {stamp}{rule}{detail}")
    return "\n".join(lines)


def explain(controller: ExpertController, fact_type: str, **fields: Any) -> str:
    """``/why <fact_type> [k=v ...]`` — render the proof tree."""
    if not fact_type:
        return "Usage: /why <fact_type> [field=value ...]"
    chainer = controller.middleware.make_backward_chainer()
    pattern = _pattern_from_kv(fact_type, fields)
    tree = chainer.why(pattern)
    return render_tree(tree)


def prove(controller: ExpertController, fact_type: str, **fields: Any) -> str:
    """``/prove <fact_type> [k=v ...]`` — render the proof tree."""
    if not fact_type:
        return "Usage: /prove <fact_type> [field=value ...]"
    chainer = controller.middleware.make_backward_chainer()
    pattern = _pattern_from_kv(fact_type, fields)
    tree = chainer.prove(pattern)
    return render_tree(tree)


def assert_fact(controller: ExpertController, fact_type: str, **fields: Any) -> str:
    """Inject a fact into working memory."""
    fact = controller.middleware.engine.assert_fact(
        Fact(fact_type=fact_type, data=dict(fields))
    )
    return f"Asserted {fact_type}#{fact.id}: {dict(fields)}"


def run(controller: ExpertController) -> str:
    """Run the engine to a fixed point against the current memory."""
    result = controller.middleware.engine.run()
    lines = [
        f"Engine ran {result.iterations} iteration(s)"
        f"{' (truncated)' if result.truncated else ''}.",
        f"Activations fired: {len(result.activations)}",
        f"Denied: {result.denied}",
    ]
    if result.deny_reasons:
        lines.append(f"Deny reasons: {result.deny_reasons}")
    return "\n".join(lines)


def example() -> str:
    """Print a starter rule YAML."""
    return _EXAMPLE_RULE


def lint(controller: ExpertController) -> str:
    """Run the rulebook linter and render findings as text."""
    report = lint_rules(controller.middleware.engine.rules)
    return render_lint_report(report)


def dry_run(controller: ExpertController, fact_type: str, **fields: Any) -> str:
    """Assert a fact, run the engine, then retract — show what would happen.

    Unlike ``/expert assert`` + ``/expert run``, the asserted fact is
    rolled back at the end so working memory remains untouched. The
    ``denials`` / ``modifications`` / ``approvals`` counters are NOT
    bumped (we restore them to their pre-call values).
    """
    if not fact_type:
        return "Usage: /expert dry-run <fact_type> [field=value ...]"
    mw = controller.middleware
    engine = mw.engine
    before_counters = dict(mw.counters)
    asserted = engine.assert_fact(Fact(fact_type=fact_type, data=dict(fields)))
    try:
        result = engine.run()
    finally:
        engine.retract(asserted.id)
        mw._denials = before_counters["denials"]
        mw._modifications = before_counters["modifications"]
        mw._approvals = before_counters["approvals"]
    lines = [
        f"Dry-run: asserted {fact_type}#{asserted.id} {dict(fields)!r}",
        f"  Iterations: {result.iterations}"
        f"{' (truncated)' if result.truncated else ''}",
        f"  Activations fired: {len(result.activations)}"
        + (
            f" — {', '.join(a.rule.name for a in result.activations)}"
            if result.activations
            else ""
        ),
        f"  Denied: {result.denied}",
    ]
    if result.deny_reasons:
        lines.append(f"  Deny reasons: {result.deny_reasons}")
    if result.actions.modifications:
        lines.append(f"  Modifications: {result.actions.modifications}")
    if result.actions.approvals_required:
        lines.append(f"  Approvals required: {result.actions.approvals_required}")
    lines.append("(fact retracted; counters restored)")
    return "\n".join(lines)


def memory_stats(controller: ExpertController) -> str:
    """Show working-memory contents (by fact type)."""
    stats = controller.middleware.engine.memory.stats()
    if not stats:
        return "Working memory is empty."
    lines = ["Working memory:"]
    for ft, n in sorted(stats.items()):
        lines.append(f"  {ft}: {n} fact(s)")
    return "\n".join(lines)


def clear_memory(controller: ExpertController) -> str:
    """Wipe working memory. Counters and rules stay."""
    controller.middleware.engine.memory.clear()
    return "Cleared working memory."


__all__ = [
    "assert_fact",
    "clear_memory",
    "dry_run",
    "example",
    "explain",
    "lint",
    "list_rules",
    "memory_stats",
    "prove",
    "reload",
    "run",
    "set_enabled",
    "show_rule",
    "status",
    "trace",
]
