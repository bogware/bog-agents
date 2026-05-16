"""Stand-alone controller for the ``/expert``, ``/why``, ``/prove`` commands.

Backs the ``/expert``, ``/expert trace``, ``/why``, and ``/prove`` slash
commands. All user-facing logic lives here so the TUI handlers in ``app.py`` are
trivially thin (4-6 lines each) and the feature is testable without the
Textual app. One :class:`ExpertController` instance is created per
working directory and cached in :data:`_CONTROLLERS`.

The controller owns one :class:`ExpertRulesMiddleware` instance, so facts
asserted by tool calls and facts asserted by the user via ``/expert assert``
share the same working memory.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bog_agents.middleware.expert_engine import (
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    lint as lint_rules,
    render_report as render_lint_report,
)
from bog_agents.middleware.expert_engine.backward import render_tree
from bog_agents.middleware.expert_rules import ExpertRulesMiddleware

# ---------------------------------------------------------------------------
# Singleton registry
# ---------------------------------------------------------------------------

_CONTROLLERS: dict[Path, ExpertController] = {}


def get_controller(working_dir: Path | str) -> ExpertController:
    """Return the (per-cwd) singleton controller.

    Args:
        working_dir: Project root. Different roots get independent
            controllers — useful when the CLI hops between repos via
            ``/cd``.
    """
    key = Path(working_dir).resolve()
    if key not in _CONTROLLERS:
        _CONTROLLERS[key] = ExpertController(working_dir=key)
    return _CONTROLLERS[key]


def reset_controllers() -> None:
    """Drop every cached controller. Test-only helper."""
    _CONTROLLERS.clear()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ExpertController:
    """Slash-command-facing facade around :class:`ExpertRulesMiddleware`.

    Args:
        working_dir: Project root (rules are loaded from
            ``<working_dir>/.bog-agents/expert_rules/``).
        middleware: Optional preconstructed middleware. Tests use this
            to inject programmatic rules.
    """

    def __init__(
        self,
        *,
        working_dir: Path,
        middleware: ExpertRulesMiddleware | None = None,
    ) -> None:
        self._working_dir = working_dir
        self._middleware = middleware or ExpertRulesMiddleware(
            working_dir=working_dir,
            enabled=False,  # start disabled — explicit opt-in via /expert on
        )

    # ------------------------------------------------------------------
    # Used by app.py to register the middleware with create_agent
    # ------------------------------------------------------------------

    @property
    def middleware(self) -> ExpertRulesMiddleware:
        """The underlying middleware (exposed for ``create_agent`` registration)."""
        return self._middleware

    # ------------------------------------------------------------------
    # Command surface (each returns formatted text)
    # ------------------------------------------------------------------

    def status(self) -> str:
        """Human-readable status — used by ``/expert`` with no subcommand."""
        rule_count = len(self._middleware.engine.rules)
        counters = self._middleware.counters
        state = "ON" if self._middleware.enabled else "OFF"
        rules_dir = self._working_dir / ".bog-agents" / "expert_rules"
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
            for rule in self._middleware.engine.rules:
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

    def set_enabled(self, on: bool) -> str:
        """Toggle the engine on/off."""
        self._middleware.set_enabled(on)
        return f"Expert mode: {'ON' if on else 'OFF'}"

    def reload(self) -> str:
        """Force a rule reload from disk."""
        count, err = self._middleware.reload()
        if err:
            return f"Reload completed with errors:\n  {err}\n\n(kept the previous {count} rules live)"
        return f"Reloaded {count} rule(s) from disk."

    def list_rules(self) -> str:
        """List every loaded rule's name + summary."""
        rules = self._middleware.engine.rules
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

    def show_rule(self, name: str) -> str:
        """Show one rule's source file path + summary."""
        if not name:
            return "Usage: /expert show <rule-name>"
        match = next(
            (r for r in self._middleware.engine.rules if r.name == name),
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

    def trace(self, limit: int = 50) -> str:
        """Render the last engine run trace (up to *limit* entries)."""
        entries = self._middleware.last_trace()
        if not entries:
            return "No trace available — no tool call has run through the engine yet."
        lines = [f"Last engine trace ({len(entries)} entries):"]
        for e in entries[-limit:]:
            stamp = f"[{e['kind']}]"
            rule = f" {e['rule']}" if e["rule"] else ""
            detail = f" — {e['detail']}" if e["detail"] else ""
            lines.append(f"  {stamp}{rule}{detail}")
        return "\n".join(lines)

    def explain(self, fact_type: str, **fields: Any) -> str:
        """``/why <fact_type> [k=v ...]`` — render the proof tree."""
        if not fact_type:
            return "Usage: /why <fact_type> [field=value ...]"
        chainer = self._middleware.make_backward_chainer()
        pattern = _pattern_from_kv(fact_type, fields)
        tree = chainer.why(pattern)
        return render_tree(tree)

    def prove(self, fact_type: str, **fields: Any) -> str:
        """``/prove <fact_type> [k=v ...]`` — render the proof tree."""
        if not fact_type:
            return "Usage: /prove <fact_type> [field=value ...]"
        chainer = self._middleware.make_backward_chainer()
        pattern = _pattern_from_kv(fact_type, fields)
        tree = chainer.prove(pattern)
        return render_tree(tree)

    def assert_fact(self, fact_type: str, **fields: Any) -> str:
        """Inject a fact into working memory (debug / demo).

        Used by ``/expert assert <fact_type> k=v ...`` so users can drive
        the engine without an actual tool call.
        """
        fact = self._middleware.engine.assert_fact(
            Fact(fact_type=fact_type, data=dict(fields))
        )
        return f"Asserted {fact_type}#{fact.id}: {dict(fields)}"

    def run(self) -> str:
        """Run the engine to a fixed point against the current memory."""
        result = self._middleware.engine.run()
        lines = [
            f"Engine ran {result.iterations} iteration(s)"
            f"{' (truncated)' if result.truncated else ''}.",
            f"Activations fired: {len(result.activations)}",
            f"Denied: {result.denied}",
        ]
        if result.deny_reasons:
            lines.append(f"Deny reasons: {result.deny_reasons}")
        return "\n".join(lines)

    @staticmethod
    def example() -> str:
        """Print a starter rule YAML."""
        return _EXAMPLE_RULE

    def lint(self) -> str:
        """Run the rulebook linter and render findings as text."""
        report = lint_rules(self._middleware.engine.rules)
        return render_lint_report(report)

    def dry_run(self, fact_type: str, **fields: Any) -> str:
        """Assert a fact, run the engine, then retract — show what would happen.

        Unlike ``/expert assert`` + ``/expert run``, the asserted fact is
        rolled back at the end so working memory remains untouched. The
        ``denials`` / ``modifications`` / ``approvals`` counters are NOT
        bumped (we restore them to their pre-call values). This is the
        right command for "would my rule fire against this kind of call?"
        without polluting later traces.
        """
        if not fact_type:
            return "Usage: /expert dry-run <fact_type> [field=value ...]"
        engine = self._middleware.engine
        before_counters = dict(self._middleware.counters)
        asserted = engine.assert_fact(Fact(fact_type=fact_type, data=dict(fields)))
        try:
            result = engine.run()
        finally:
            engine.retract(asserted.id)
            # Restore counters so dry-run doesn't pollute the session view.
            self._middleware._denials = before_counters["denials"]
            self._middleware._modifications = before_counters["modifications"]
            self._middleware._approvals = before_counters["approvals"]
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

    def memory_stats(self) -> str:
        """Show working-memory contents (by fact type)."""
        stats = self._middleware.engine.memory.stats()
        if not stats:
            return "Working memory is empty."
        lines = ["Working memory:"]
        for ft, n in sorted(stats.items()):
            lines.append(f"  {ft}: {n} fact(s)")
        return "\n".join(lines)

    def clear_memory(self) -> str:
        """Wipe working memory. Counters and rules stay."""
        self._middleware.engine.memory.clear()
        return "Cleared working memory."

    # ------------------------------------------------------------------
    # Slash-command dispatcher (one entry point per command surface)
    # ------------------------------------------------------------------

    def handle_expert(self, args: str) -> str:
        """Dispatch ``/expert [subcommand …]``."""
        sub, rest = _split_subcommand(args)
        if not sub:
            return self.status()
        if sub == "on":
            return self.set_enabled(True)
        if sub == "off":
            return self.set_enabled(False)
        if sub == "reload":
            return self.reload()
        if sub in ("list", "rules"):
            return self.list_rules()
        if sub == "show":
            return self.show_rule(rest.strip())
        if sub == "trace":
            try:
                limit = int(rest.strip()) if rest.strip() else 50
            except ValueError:
                limit = 50
            return self.trace(limit=limit)
        if sub == "memory":
            return self.memory_stats()
        if sub == "clear":
            return self.clear_memory()
        if sub == "assert":
            ft, fields = _parse_pattern_args(rest)
            if not ft:
                return "Usage: /expert assert <fact_type> [field=value ...]"
            return self.assert_fact(ft, **fields)
        if sub == "run":
            return self.run()
        if sub == "example":
            return self.example()
        if sub == "lint":
            return self.lint()
        if sub in ("dry-run", "dryrun"):
            ft, fields = _parse_pattern_args(rest)
            return self.dry_run(ft, **fields)
        if sub == "status":
            return self.status()
        return (
            f"Unknown /expert subcommand: '{sub}'.\n\n"
            "Try one of:\n"
            "  /expert                              — show status\n"
            "  /expert on|off                       — toggle the engine\n"
            "  /expert list                         — list loaded rules\n"
            "  /expert show <name>                  — show a rule\n"
            "  /expert lint                         — static analysis of the rulebook\n"
            "  /expert trace [N]                    — last run trace\n"
            "  /expert memory                       — working-memory contents\n"
            "  /expert clear                        — wipe working memory\n"
            "  /expert assert <fact_type> k=v ...    — inject a fact\n"
            "  /expert dry-run <fact_type> k=v ...   — simulate without persisting\n"
            "  /expert run                          — run engine to fixed point\n"
            "  /expert reload                       — reload rules from disk\n"
            "  /expert example                      — print a starter rule"
        )

    def handle_why(self, args: str) -> str:
        """Dispatch ``/why <fact_type> [field=value ...]``."""
        ft, fields = _parse_pattern_args(args)
        return self.explain(ft, **fields)

    def handle_prove(self, args: str) -> str:
        """Dispatch ``/prove <fact_type> [field=value ...]``."""
        ft, fields = _parse_pattern_args(args)
        return self.prove(ft, **fields)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _split_subcommand(text: str) -> tuple[str, str]:
    """Split ``"on rest of args"`` into ``("on", "rest of args")``."""
    text = text.strip()
    if not text:
        return ("", "")
    parts = text.split(None, 1)
    if len(parts) == 1:
        return (parts[0].lower(), "")
    return (parts[0].lower(), parts[1])


def _parse_pattern_args(text: str) -> tuple[str, dict[str, Any]]:
    """Parse ``"fact_type k1=v1 k2=v2"`` into ``("fact_type", {k1: v1, k2: v2})``.

    Values that look like JSON literals (``true``, ``false``, ``null``,
    numbers, or quoted strings) are decoded via :func:`json.loads`; anything
    else stays a string. ``shlex`` handles quoted multi-word values.
    """
    if not text.strip():
        return ("", {})
    try:
        tokens = shlex.split(text)
    except ValueError:
        return (text.strip(), {})
    if not tokens:
        return ("", {})
    fact_type = tokens[0]
    fields: dict[str, Any] = {}
    for tok in tokens[1:]:
        if "=" not in tok:
            continue
        key, _, raw = tok.partition("=")
        fields[key] = _coerce_value(raw)
    return (fact_type, fields)


def _coerce_value(raw: str) -> Any:  # noqa: ANN401 — CLI values are intentionally untyped
    """Best-effort JSON-ish coercion of a CLI value."""
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _pattern_from_kv(fact_type: str, fields: dict[str, Any]) -> Pattern:
    """Build an equality :class:`Pattern` from keyword-arg fields."""
    preds = tuple(Predicate(field=k, op=PredicateOp.EQ, value=v) for k, v in fields.items())
    return Pattern(fact_type=fact_type, predicates=preds)


# ---------------------------------------------------------------------------
# Built-in starter rule (printed by ``/expert example``)
# ---------------------------------------------------------------------------


_EXAMPLE_RULE = """# Example rule — save to .bog-agents/expert_rules/example.yaml,
# then run /expert reload.

- name: block_force_push_to_main
  description: Block force-pushes to main/master.
  salience: 100
  when:
    - tool_call:
        name: shell_execute
        command:
          matches: 'git push.*--force.*(main|master)'
  then:
    - deny: "Force-push to main is prohibited by policy."
    - audit_log:
        event: prod_force_push_blocked

- name: budget_brake
  description: Brake on session spend > $5.
  salience: 90
  when:
    - session:
        cost_usd:
          gt: 5.0
  then:
    - require_approval:
        gate: "Cost exceeded $5.00 — continue?"
        risk: high
"""


# ---------------------------------------------------------------------------
# Callable convenience (used by app.py handlers)
# ---------------------------------------------------------------------------


def dispatch(command_text: str, working_dir: Path | str) -> str:
    """Top-level dispatcher for the three slash commands.

    Args:
        command_text: Raw input including leading slash, e.g.
            ``"/expert on"`` or ``"/why tool_call name=shell"``.
        working_dir: Project root.

    Returns:
        Plain text to render in the TUI.
    """
    controller = get_controller(working_dir)
    text = command_text.strip()
    if text.startswith("/expert"):
        return controller.handle_expert(text[len("/expert"):].strip())
    if text.startswith("/why"):
        return controller.handle_why(text[len("/why"):].strip())
    if text.startswith("/prove"):
        return controller.handle_prove(text[len("/prove"):].strip())
    return f"Unknown expert command: {text}"


# Re-exported for type-checkers and downstream users:
__all__ = [
    "ExpertController",
    "dispatch",
    "get_controller",
    "reset_controllers",
]


# Silence "unused" import lint for Callable in the type hints above
_ = Callable
