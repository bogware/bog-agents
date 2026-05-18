"""``/trace-mind replay <event_id>`` — time-travel from a rule fire (Q3).

Read a recorded causal session, locate the rule fire (or tool call)
the user wants to revisit, and re-run the **rules engine** over the
same facts with a modified policy. Render a diff between the
original outcome and the counterfactual outcome.

What "replay" actually means here
---------------------------------

Full agent replay (re-issuing model calls, re-running tools) is out
of scope — it needs durable checkpoints, deterministic LLM
replies, and a sandbox we don't have yet. What we *can* do today,
cheaply and deterministically, is replay **just the rules engine**:

* Reconstruct the working memory from the causal log
  (``tool_call`` events become :class:`Fact` instances).
* Mutate the rulebook (drop a named rule, add a YAML-encoded rule).
* Run the engine to a fixed point.
* Compare the actions fired before and after.

This is enough to answer the most common time-travel question:
"if rule X had been disabled / different, would the agent have been
blocked / approved / modified differently?"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from bog_agents.middleware.expert_engine import (
    Action,
    ActionKind,
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)
from bog_agents.middleware.expert_engine.engine import ExpertEngine
from bog_agents.middleware.expert_engine.loader import (
    RuleLoadError,
    load_rules_from_string,
)

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    EventKind,
    load_session,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayInput:
    """Inputs to one :func:`replay` call.

    Attributes:
        anchor_event_id: The event in the causal log we're replaying
            "from". Must be the id of a ``rule_fire`` or ``tool_call``
            event.
        drop_rules: Set of rule names to omit from the replay
            rulebook. Empty by default.
        add_rules_yaml: Optional YAML body whose rules are added on
            top of the loaded set after drops.
    """

    anchor_event_id: int
    drop_rules: tuple[str, ...] = ()
    add_rules_yaml: str = ""


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """Single rule-engine outcome (before-or-after).

    Note on ``denied`` shape: the underlying engine's ``RunResult.denied``
    is a *boolean* (any rule fired a DENY action), not a count. We
    keep an int field here so the renderer can subtract before/after
    cleanly — the value is just ``1`` or ``0``.
    """

    activations: tuple[str, ...]
    """Names of rules that fired, in order."""
    denials: int
    deny_reasons: tuple[str, ...]
    modifications: int
    approvals_required: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """End-to-end outcome of one replay."""

    anchor: CausalEvent
    facts_used: tuple[Fact, ...]
    before: ReplayOutcome
    after: ReplayOutcome
    rule_count_before: int
    rule_count_after: int
    error: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        """True iff at least one outcome field differs between before/after."""
        return (
            self.before.activations != self.after.activations
            or self.before.denials != self.after.denials
            or self.before.modifications != self.after.modifications
            or self.before.approvals_required != self.after.approvals_required
        )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(
    *,
    session_id: str,
    working_dir: Path,
    input: ReplayInput,
    rules: list[Rule],
) -> ReplayResult:
    """Run the rules engine on the anchor's facts with + without changes.

    Args:
        session_id: Causal session to load.
        working_dir: Project root.
        input: Anchor event + rule changes.
        rules: The current ruleset to start from.
    """
    events = load_session(working_dir, session_id)
    if not events:
        return _error(
            session_id,
            input,
            error=f"Session {session_id} has no recorded events.",
        )
    anchor = _find_anchor(events, input.anchor_event_id)
    if anchor is None:
        return _error(
            session_id,
            input,
            error=(
                f"Event #{input.anchor_event_id} not found in session "
                f"{session_id}. Use /trace-mind last to find a valid id."
            ),
        )

    facts = _facts_from_anchor_ancestry(anchor, events)
    if not facts:
        return _error(
            session_id,
            input,
            anchor=anchor,
            error=(
                f"No tool_call ancestor found for event #{anchor.id}; "
                "can't reconstruct facts to replay against."
            ),
        )

    # Build before/after rulebooks.
    try:
        after_rules, notes = _apply_rule_changes(rules, input)
    except _RuleChangeError as exc:
        return _error(
            session_id,
            input,
            anchor=anchor,
            error=str(exc),
        )

    before = _run_engine(rules, facts)
    after = _run_engine(after_rules, facts)
    return ReplayResult(
        anchor=anchor,
        facts_used=tuple(facts),
        before=before,
        after=after,
        rule_count_before=len(rules),
        rule_count_after=len(after_rules),
        notes=tuple(notes),
    )


def _find_anchor(events: list[CausalEvent], event_id: int) -> CausalEvent | None:
    for e in events:
        if e.id == event_id:
            return e
    return None


def _facts_from_anchor_ancestry(
    anchor: CausalEvent, events: list[CausalEvent]
) -> list[Fact]:
    """Reconstruct the working-memory facts that produced *anchor*.

    Walks the anchor's ancestry, picking up every ``tool_call`` event
    and turning it into a :class:`Fact` of type ``tool_call``. The
    fact's ``data`` is the event payload's ``args_keys`` mapped to
    placeholder values plus ``name`` taken from ``actor``.

    For the MVP we treat every tool_call as ``tool_call(name=<actor>)``
    with whatever payload keys we know about. This is enough to
    re-evaluate rule predicates over name/op patterns; richer field
    reconstruction is a follow-up.
    """
    by_id = {e.id: e for e in events}
    facts: list[Fact] = []
    seen: set[int] = set()
    frontier: list[int] = [anchor.id]
    while frontier:
        cur_id = frontier.pop(0)
        if cur_id in seen:
            continue
        seen.add(cur_id)
        cur = by_id.get(cur_id)
        if cur is None:
            continue
        if cur.kind == EventKind.TOOL_CALL:
            facts.append(_event_to_fact(cur))
        frontier.extend(p for p in cur.parent_ids if p not in seen)
    return facts


def _event_to_fact(event: CausalEvent) -> Fact:
    """Synthesise a tool_call Fact from a TOOL_CALL event.

    The trace records arg *keys* (not values, since arg values can be
    huge or sensitive). For each key we fill in a sentinel string so
    predicates that just check key presence (``op: exists``) work,
    while value-comparison predicates degrade gracefully — they'll
    fail to match, which is the conservative behavior.
    """
    data: dict[str, Any] = {"name": event.actor}
    for key in event.payload.get("args_keys", ()) or ():
        data.setdefault(str(key), f"<trace:{event.id}>")
    return Fact(fact_type="tool_call", data=data)


# ---------------------------------------------------------------------------
# Rule mutation
# ---------------------------------------------------------------------------


class _RuleChangeError(ValueError):
    """Raised when the user-supplied rule modifications can't be applied."""


def _apply_rule_changes(
    rules: list[Rule], input: ReplayInput
) -> tuple[list[Rule], list[str]]:
    """Return ``(modified_rules, notes)`` after applying *input*."""
    notes: list[str] = []
    drop_set = {name for name in input.drop_rules if name}
    after = [r for r in rules if r.name not in drop_set]
    actually_dropped = {r.name for r in rules} & drop_set
    missing_drops = drop_set - actually_dropped
    if missing_drops:
        notes.append(
            f"warning: --no-rule did not match any loaded rule: "
            f"{', '.join(sorted(missing_drops))}"
        )
    if input.add_rules_yaml.strip():
        try:
            extra = load_rules_from_string(input.add_rules_yaml)
        except RuleLoadError as exc:
            msg = f"Could not parse --with-rule YAML: {exc}"
            raise _RuleChangeError(msg) from exc
        existing_names = {r.name for r in after}
        for rule in extra:
            if rule.name in existing_names:
                msg = (
                    f"--with-rule {rule.name!r} collides with an existing "
                    "rule name. Drop the original first with --no-rule, "
                    "or rename your replacement."
                )
                raise _RuleChangeError(msg)
        after.extend(extra)
        notes.append(f"added {len(extra)} rule(s) from --with-rule")
    return after, notes


def _run_engine(rules: list[Rule], facts: list[Fact]) -> ReplayOutcome:
    """Run the engine over *facts* + *rules* and summarise the outcome."""
    engine = ExpertEngine(rules=rules)
    for fact in facts:
        engine.assert_fact(fact)
    result = engine.run()
    return ReplayOutcome(
        activations=tuple(a.rule.name for a in result.activations),
        denials=1 if result.denied else 0,
        deny_reasons=tuple(result.deny_reasons),
        modifications=len(result.actions.modifications),
        approvals_required=len(result.actions.approvals_required),
    )


def _error(
    session_id: str,
    input: ReplayInput,
    *,
    anchor: CausalEvent | None = None,
    error: str = "",
) -> ReplayResult:
    """Build a ReplayResult with the error filled in."""
    empty = ReplayOutcome(
        activations=(),
        denials=0,
        deny_reasons=(),
        modifications=0,
        approvals_required=0,
    )
    return ReplayResult(
        anchor=anchor
        or CausalEvent(
            id=input.anchor_event_id,
            kind=EventKind.NOTE,
            timestamp=0.0,
            actor="",
            summary="",
        ),
        facts_used=(),
        before=empty,
        after=empty,
        rule_count_before=0,
        rule_count_after=0,
        error=error,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_result(result: ReplayResult) -> str:
    """Format the diff between the before/after engine outcomes."""
    if result.error:
        return f"/trace-mind replay failed: {result.error}"
    lines = [
        f"== Time-travel replay from event #{result.anchor.id} ==",
        f"  Anchor: [{result.anchor.kind.value}] "
        f"{result.anchor.actor}: {result.anchor.summary}",
        f"  Facts:  {len(result.facts_used)} tool_call fact(s) reconstructed",
        f"  Rules:  {result.rule_count_before} → {result.rule_count_after}",
        "",
    ]
    for note in result.notes:
        lines.append(f"  · {note}")
    if result.notes:
        lines.append("")

    lines.append("Before (current rulebook):")
    lines.extend(_render_outcome(result.before, indent="  "))
    lines.append("")
    lines.append("After (with your changes):")
    lines.extend(_render_outcome(result.after, indent="  "))
    lines.append("")
    if result.changed:
        lines.append("Diff: outcomes differ.")
        lines.extend(_render_diff(result.before, result.after, indent="  "))
    else:
        lines.append("Diff: identical outcomes — your changes had no effect.")
    return "\n".join(lines)


def _render_outcome(outcome: ReplayOutcome, *, indent: str = "") -> list[str]:
    if not outcome.activations and outcome.denials == 0 and outcome.modifications == 0:
        return [f"{indent}(no rules fired)"]
    lines = [
        f"{indent}Activations:    {', '.join(outcome.activations) or '(none)'}",
        f"{indent}Denials:        {outcome.denials}",
    ]
    if outcome.deny_reasons:
        lines.append(f"{indent}Deny reasons:   {', '.join(outcome.deny_reasons)}")
    if outcome.modifications:
        lines.append(f"{indent}Modifications:  {outcome.modifications}")
    if outcome.approvals_required:
        lines.append(f"{indent}Approvals:      {outcome.approvals_required}")
    return lines


def _render_diff(
    before: ReplayOutcome, after: ReplayOutcome, *, indent: str = ""
) -> list[str]:
    lines: list[str] = []
    added = [a for a in after.activations if a not in before.activations]
    removed = [a for a in before.activations if a not in after.activations]
    if added:
        lines.append(f"{indent}+ activations: {', '.join(added)}")
    if removed:
        lines.append(f"{indent}- activations: {', '.join(removed)}")
    if before.denials != after.denials:
        lines.append(f"{indent}denials: {before.denials} → {after.denials}")
    if before.modifications != after.modifications:
        lines.append(
            f"{indent}modifications: {before.modifications} → {after.modifications}"
        )
    if before.approvals_required != after.approvals_required:
        lines.append(
            f"{indent}approvals: {before.approvals_required} → {after.approvals_required}"
        )
    return lines


# ---------------------------------------------------------------------------
# Slash-command dispatch
# ---------------------------------------------------------------------------


def dispatch(
    command_text: str,
    *,
    working_dir: Path,
    session_id: str | None,
    rules_provider: RulesProvider,
) -> str:
    """Top-level ``/trace-mind replay …`` dispatch.

    Args:
        command_text: Raw slash input. Expected to start with
            ``/trace-mind replay`` (the outer ``/causal`` dispatcher in
            ``causal/controller.py`` already handles the other
            subcommands; this function exists so the time-travel
            logic stays cohesive).
        working_dir: Project root.
        session_id: The active causal session id (or ``None`` for
            "use the latest").
        rules_provider: Callable returning the current rulebook —
            normally the expert controller's middleware.
    """
    rest = command_text.strip()
    for prefix in (
        "/trace-mind replay",
        "/trace-mind-replay",
        "/trace-mind replay",
        "/causal-replay",
    ):
        if rest.startswith(prefix):
            rest = rest[len(prefix) :].strip()
            break
    if not rest or rest.lower() in ("help", "?"):
        return _help_text()
    parsed = _parse_args(rest)
    if isinstance(parsed, str):
        return parsed
    anchor, drops, add_yaml = parsed
    resolved_id = session_id
    if resolved_id is None or resolved_id == "latest":
        from bog_agents_cli.causal.ledger import list_sessions

        sessions = list_sessions(working_dir)
        if not sessions:
            return "No causal sessions found. Run /trace-mind on and a turn first."
        resolved_id = sessions[0]
    try:
        rules = list(rules_provider())
    except Exception as exc:
        logger.exception("time-travel: rules_provider failed")
        return f"Could not load active rules: {exc}"
    result = replay(
        session_id=resolved_id,
        working_dir=working_dir,
        input=ReplayInput(
            anchor_event_id=anchor,
            drop_rules=tuple(drops),
            add_rules_yaml=add_yaml,
        ),
        rules=rules,
    )
    return render_result(result)


def _parse_args(
    rest: str,
) -> tuple[int, list[str], str] | str:
    """Parse ``<event-id> [--no-rule X]* [--with-rule <yaml-or-path>]``."""
    tokens = rest.split()
    if not tokens:
        return (
            "Usage: /trace-mind replay <event_id> [--no-rule NAME] [--with-rule PATH]"
        )
    try:
        anchor = int(tokens[0])
    except ValueError:
        return f"Invalid event id: {tokens[0]!r} (expected an integer)."
    drops: list[str] = []
    add_yaml = ""
    i = 1
    while i < len(tokens):
        flag = tokens[i]
        if flag == "--no-rule":
            if i + 1 >= len(tokens):
                return "Missing value after --no-rule."
            drops.append(tokens[i + 1])
            i += 2
            continue
        if flag == "--with-rule":
            if i + 1 >= len(tokens):
                return "Missing value after --with-rule."
            value = " ".join(tokens[i + 1 :])
            # Treat as a path first; fall back to literal YAML.
            path = Path(value)
            if path.is_file() and path.suffix in (".yaml", ".yml"):
                try:
                    add_yaml = path.read_text(encoding="utf-8")
                except OSError as exc:
                    return f"Could not read {path}: {exc}"
            else:
                add_yaml = value
            break
        return f"Unknown flag: {flag!r}"
    return anchor, drops, add_yaml


def _help_text() -> str:
    return (
        "/trace-mind replay <event_id> — time-travel from a rule fire.\n\n"
        "Usage:\n"
        "  /trace-mind replay <id>                       — re-run rules over\n"
        "                                              the anchor's facts\n"
        "  /trace-mind replay <id> --no-rule NAME ...    — drop named rule(s)\n"
        "  /trace-mind replay <id> --with-rule PATH      — add rules from\n"
        "                                              a YAML file or inline body\n\n"
        "Outputs a before/after diff of activations, denials, mods, approvals.\n"
        "Use /trace-mind last to find event ids worth replaying."
    )


# Typing alias — kept stringly to avoid forward-ref noise.
RulesProvider = Any  # callable returning Iterable[Rule]


# ---------------------------------------------------------------------------
# Convenience exports
# ---------------------------------------------------------------------------


# Re-export the engine value types so tests can build inputs without
# pulling them directly out of the SDK module.
_SDK_VALUE_REEXPORTS = (
    Action,
    ActionKind,
    Fact,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)

# Quiet "imported but unused" hint when ruff doesn't track __all__.
yaml  # noqa: B018


__all__ = [
    "ReplayInput",
    "ReplayOutcome",
    "ReplayResult",
    "RulesProvider",
    "dispatch",
    "render_result",
    "replay",
]
