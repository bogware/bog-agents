"""Parsing + placeholder helpers shared by the ``expert`` sub-modules.

Extracted from ``expert_controller.py`` during the K4 split so each
feature module (``status``, ``write``, ``wizard``, ``propose``,
``watch``) can import the small parsing utilities without pulling the
whole controller back in. Behavior is unchanged — these are the same
functions that previously lived at the bottom of the controller file.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from bog_agents.middleware.expert_engine import Pattern, Predicate, PredicateOp


class _NullModel:
    """Placeholder for the wizard's "no-intent → help" path.

    ``run_wizard`` only calls ``model.invoke`` when there's an intent
    to author; the no-intent help branch returns formatted text without
    going to the LLM. We hand this stub in so the signature stays
    consistent and tests don't have to mock a real model just to print
    the category help.
    """

    def invoke(self, _messages: list) -> Any:  # noqa: ANN401, PLR6301
        msg = "_NullModel.invoke should never be called"
        raise AssertionError(msg)


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


__all__ = [
    "_EXAMPLE_RULE",
    "_NullModel",
    "_coerce_value",
    "_parse_pattern_args",
    "_pattern_from_kv",
    "_split_subcommand",
]
