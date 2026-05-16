"""YAML rule loader.

Parses ``*.yaml`` / ``*.yml`` files (or in-memory strings) into
:class:`Rule` objects with helpful error messages that name the file
and the offending rule.

Rule file grammar (one or many rules per file)::

    - name: prod_force_push_gate
      description: Block force-push to main on prod environments.
      salience: 100
      once: false
      when:
        - tool_call:
            command:
              matches: 'git push.*--force.*(main|master)'
            name: shell_execute
        - context:
            env:
              in: [prod, production]
      then:
        - deny:
            reason: "Force-push to main on prod is prohibited"
        - audit_log:
            event: prod_force_push_blocked
        - notify:
            channel: slack
            severity: high

Each entry under ``when`` is a single-key dict whose key is the
``fact_type``. Its value is a dict of ``field -> predicate``. A scalar
shorthand (``field: value``) is treated as ``eq``. A nested map uses an
explicit operator (``matches``, ``in``, ``gt``, …). Two special keys are
recognised under any pattern: ``$bind: name`` and ``$not: true``.

Each entry under ``then`` is a single-key dict whose key is the action
verb (``deny``, ``modify``, ``require_approval``, …) and whose value is
the action parameters.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from bog_agents.middleware.expert_engine.types import (
    Action,
    ActionKind,
    Pattern,
    Predicate,
    PredicateOp,
    Rule,
)


class RuleLoadError(ValueError):
    """Raised when a rule file is malformed.

    The exception message starts with ``"<file>:<rule-name>: "`` when
    possible, so a CLI ``except RuleLoadError as exc: print(exc)`` is
    immediately useful.
    """


# Map of YAML operator keys → PredicateOp.
_OPS: dict[str, PredicateOp] = {
    "eq": PredicateOp.EQ,
    "ne": PredicateOp.NE,
    "in": PredicateOp.IN,
    "not_in": PredicateOp.NOT_IN,
    "gt": PredicateOp.GT,
    "gte": PredicateOp.GTE,
    "lt": PredicateOp.LT,
    "lte": PredicateOp.LTE,
    "matches": PredicateOp.MATCHES,
    "contains": PredicateOp.CONTAINS,
    "exists": PredicateOp.EXISTS,
    "missing": PredicateOp.MISSING,
}

# Map of YAML action verb → ActionKind.
_ACTIONS: dict[str, ActionKind] = {
    "deny": ActionKind.DENY,
    "modify": ActionKind.MODIFY,
    "require_approval": ActionKind.REQUIRE_APPROVAL,
    "notify": ActionKind.NOTIFY,
    "audit_log": ActionKind.AUDIT_LOG,
    "assert_fact": ActionKind.ASSERT_FACT,
    "retract_fact": ActionKind.RETRACT_FACT,
    "route_to_subagent": ActionKind.ROUTE_TO_SUBAGENT,
    "ask_llm": ActionKind.ASK_LLM,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_rule_file(path: Path) -> list[Rule]:
    """Load every rule from a single YAML file.

    Args:
        path: File to read. Must exist.

    Returns:
        Rules in declaration order.

    Raises:
        RuleLoadError: If the file is missing, unparseable, or any rule
            is malformed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"{path}: cannot read rule file ({exc})"
        raise RuleLoadError(msg) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"{path}: invalid YAML ({exc})"
        raise RuleLoadError(msg) from exc
    return _rules_from_doc(data, source=str(path))


def load_rules_from_dir(directory: Path) -> list[Rule]:
    """Load every ``*.yaml`` / ``*.yml`` rule file in *directory*.

    Files are loaded in sorted filename order. Subdirectories are
    ignored. Returns an empty list if the directory does not exist.

    Raises:
        RuleLoadError: If any file fails to parse. Other files are
            *not* skipped — the first failure aborts the load so a
            broken file never silently disables half a rulebook.
    """
    if not directory.is_dir():
        return []
    rules: list[Rule] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix.lower() in {".yaml", ".yml"}:
            rules.extend(load_rule_file(entry))
    return rules


def load_rules_from_string(text: str, *, source: str = "<string>") -> list[Rule]:
    """Parse a YAML string into rules. Mostly used in tests."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"{source}: invalid YAML ({exc})"
        raise RuleLoadError(msg) from exc
    return _rules_from_doc(data, source=source)


# ---------------------------------------------------------------------------
# Document → rules
# ---------------------------------------------------------------------------


def _rules_from_doc(doc: Any, *, source: str) -> list[Rule]:
    """Convert a parsed YAML document into a list of :class:`Rule`."""
    if doc is None:
        return []
    if isinstance(doc, dict) and "rules" in doc:
        doc = doc["rules"]
    if not isinstance(doc, list):
        msg = f"{source}: top-level must be a list of rules"
        raise RuleLoadError(msg)
    out: list[Rule] = []
    for idx, entry in enumerate(doc):
        if not isinstance(entry, dict):
            msg = f"{source}: rule #{idx + 1}: not a mapping"
            raise RuleLoadError(msg)
        out.append(_rule_from_dict(entry, source=source, index=idx))
    return out


def _rule_from_dict(entry: dict[str, Any], *, source: str, index: int) -> Rule:
    """Convert one rule dict into a :class:`Rule`."""
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        msg = f"{source}: rule #{index + 1}: missing or empty 'name'"
        raise RuleLoadError(msg)
    try:
        return Rule(
            name=name,
            description=str(entry.get("description", "")),
            salience=int(entry.get("salience", 0)),
            once=bool(entry.get("once", False)),
            when=_patterns_from_list(entry.get("when", ()) or (), source=source, rule_name=name),
            then=_actions_from_list(entry.get("then", ()) or (), source=source, rule_name=name),
            source_file=source,
        )
    except RuleLoadError:
        raise
    except (TypeError, ValueError) as exc:
        msg = f"{source}: rule '{name}': {exc}"
        raise RuleLoadError(msg) from exc


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


def _patterns_from_list(
    raw: Iterable[Any],
    *,
    source: str,
    rule_name: str,
) -> tuple[Pattern, ...]:
    """Convert ``when:`` list into a tuple of :class:`Pattern`."""
    out: list[Pattern] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict) or len(item) != 1:
            msg = (
                f"{source}: rule '{rule_name}': when[{idx}] must be a single-key dict "
                "(e.g. - tool_call: ...)"
            )
            raise RuleLoadError(msg)
        ((fact_type, body),) = item.items()
        if not isinstance(fact_type, str):
            msg = f"{source}: rule '{rule_name}': when[{idx}] fact_type must be a string"
            raise RuleLoadError(msg)
        out.append(_pattern_from_body(fact_type, body, source=source, rule_name=rule_name, idx=idx))
    return tuple(out)


def _pattern_from_body(
    fact_type: str,
    body: Any,
    *,
    source: str,
    rule_name: str,
    idx: int,
) -> Pattern:
    """Convert one pattern body into a :class:`Pattern`."""
    body = body or {}
    if not isinstance(body, dict):
        msg = f"{source}: rule '{rule_name}': when[{idx}] body must be a mapping"
        raise RuleLoadError(msg)
    bind = body.pop("$bind", None)
    negated = bool(body.pop("$not", False))
    preds: list[Predicate] = []
    for field_name, raw in body.items():
        if not isinstance(field_name, str):
            msg = f"{source}: rule '{rule_name}': when[{idx}] field name must be a string"
            raise RuleLoadError(msg)
        preds.extend(_predicates_from_field(field_name, raw, source=source, rule_name=rule_name))
    return Pattern(
        fact_type=fact_type,
        predicates=tuple(preds),
        bind=bind if isinstance(bind, str) else None,
        negated=negated,
    )


def _predicates_from_field(
    field_name: str,
    raw: Any,
    *,
    source: str,
    rule_name: str,
) -> list[Predicate]:
    """Convert ``field: <value>`` or ``field: {op: value, ...}`` into predicates.

    Strict rule: if a value is a dict, every key must either ALL be operators
    (in which case we emit one predicate per operator), or NONE be operators
    (in which case the dict is treated as a literal value for equality). A
    mixed dict raises, because that almost certainly means a typo'd operator
    (``gtt: 5`` next to a real ``lt: 10``) which would silently never match.
    """
    if isinstance(raw, dict) and raw:
        keys = list(raw)
        op_keys = [k for k in keys if k in _OPS]
        if op_keys and len(op_keys) == len(keys):
            return [Predicate(field=field_name, op=_OPS[k], value=raw[k]) for k in keys]
        if op_keys and len(op_keys) != len(keys):
            bad = sorted(set(keys) - set(_OPS))
            msg = (
                f"{source}: rule '{rule_name}': predicate '{field_name}' mixes "
                f"operator and non-operator keys (offenders: {bad}; known operators: {sorted(_OPS)})"
            )
            raise RuleLoadError(msg)
    # Scalar / list / sub-dict without operators → equality.
    return [Predicate(field=field_name, op=PredicateOp.EQ, value=raw)]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _actions_from_list(
    raw: Iterable[Any],
    *,
    source: str,
    rule_name: str,
) -> tuple[Action, ...]:
    """Convert ``then:`` list into a tuple of :class:`Action`."""
    out: list[Action] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            # Bare verb shorthand — e.g. ``- audit_log``.
            action = _action_from_verb(item, {}, source=source, rule_name=rule_name, idx=idx)
            out.append(action)
            continue
        if not isinstance(item, dict) or len(item) != 1:
            msg = (
                f"{source}: rule '{rule_name}': then[{idx}] must be a single-key dict "
                "(e.g. - deny: ...) or a bare verb string"
            )
            raise RuleLoadError(msg)
        ((verb, params),) = item.items()
        if not isinstance(verb, str):
            msg = f"{source}: rule '{rule_name}': then[{idx}] verb must be a string"
            raise RuleLoadError(msg)
        params_dict: dict[str, Any] = {}
        if isinstance(params, dict):
            params_dict.update(params)
        elif params is None:
            pass
        elif isinstance(params, str):
            # Sugar: ``deny: "reason text"`` → ``deny: {reason: "reason text"}``.
            params_dict["reason"] = params
        else:
            params_dict["value"] = params
        out.append(
            _action_from_verb(verb, params_dict, source=source, rule_name=rule_name, idx=idx)
        )
    return tuple(out)


def _action_from_verb(
    verb: str,
    params: dict[str, Any],
    *,
    source: str,
    rule_name: str,
    idx: int,
) -> Action:
    kind = _ACTIONS.get(verb)
    if kind is None:
        msg = (
            f"{source}: rule '{rule_name}': then[{idx}] '{verb}' is not a known action "
            f"({sorted(_ACTIONS)})"
        )
        raise RuleLoadError(msg)
    return Action(kind=kind, params=params)
