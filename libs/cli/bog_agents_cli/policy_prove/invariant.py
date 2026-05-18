"""Invariant data model + YAML loader.

An invariant has the shape::

    name: no_pii_read_after_git_push
    description: After git push to main, PII tools must be blocked.
    precondition:
      fact_type: tool_call
      predicates:
        - field: name
          op: eq
          value: shell_execute
        - field: command
          op: matches
          value: 'git push.*main'
    forbidden:
      fact_type: tool_call
      predicates:
        - field: name
          op: in
          value: ["read_pii_data", "fetch_user_record"]

The model is deliberately a *shadow* of the engine's :class:`Pattern`
type. We don't reuse the engine type directly because the prover
needs to introspect patterns syntactically (subsumption checks),
which is awkward over the engine's runtime objects. The two stay in
one-to-one correspondence — see :func:`_predicate_to_engine` for the
adapter when we need to evaluate the spec against working memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from bog_agents.middleware.expert_engine import Pattern, Predicate, PredicateOp


class InvariantParseError(ValueError):
    """Raised when an invariant YAML/dict is malformed."""


@dataclass(frozen=True, slots=True)
class PredicateSpec:
    """One predicate inside a :class:`PatternSpec`.

    Equivalent to the engine's :class:`Predicate` but mutable-friendly
    for parsing. ``value`` is whatever the YAML supplied — a string,
    int, bool, list, etc. The prover does its own type coercion.
    """

    field: str
    op: PredicateOp
    value: Any = None

    def to_engine(self) -> Predicate:
        """Adapter back to the engine's :class:`Predicate`."""
        return Predicate(field=self.field, op=self.op, value=self.value)


@dataclass(frozen=True, slots=True)
class PatternSpec:
    """The invariant's view of a fact pattern.

    Always positive (no ``negated`` analogue) — invariants describe
    "X must never happen", not "X must always happen", so we don't
    need patterns that match by absence.
    """

    fact_type: str
    predicates: tuple[PredicateSpec, ...] = ()

    def to_engine(self) -> Pattern:
        """Adapter back to the engine's :class:`Pattern`."""
        return Pattern(
            fact_type=self.fact_type,
            predicates=tuple(p.to_engine() for p in self.predicates),
        )


@dataclass(frozen=True, slots=True)
class Invariant:
    """A user-supplied invariant — precondition + forbidden pattern."""

    name: str
    description: str
    precondition: PatternSpec
    forbidden: PatternSpec

    def header(self) -> str:
        """One-line summary used by the renderer."""
        return f"{self.name}: {self.description or '(no description)'}"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_invariant_from_dict(data: dict[str, Any]) -> Invariant:
    """Parse one invariant from a Python dict.

    Args:
        data: Mapping with ``name``, ``description`` (optional),
            ``precondition``, and ``forbidden`` keys. Each pattern is
            itself a dict with ``fact_type`` and an optional
            ``predicates`` list.

    Raises:
        InvariantParseError: When the dict is missing required keys
            or contains an unknown predicate op.
    """
    if not isinstance(data, dict):
        msg = f"Invariant must be a mapping, got {type(data).__name__}."
        raise InvariantParseError(msg)
    try:
        name = str(data["name"]).strip()
    except KeyError as exc:
        msg = "Invariant is missing required field 'name'."
        raise InvariantParseError(msg) from exc
    description = str(data.get("description", "")).strip()
    try:
        precondition = _parse_pattern(data["precondition"])
        forbidden = _parse_pattern(data["forbidden"])
    except KeyError as exc:
        msg = f"Invariant {name!r} is missing required key: {exc!s}."
        raise InvariantParseError(msg) from exc
    return Invariant(
        name=name,
        description=description,
        precondition=precondition,
        forbidden=forbidden,
    )


def load_invariant_from_yaml(text: str | Path) -> Invariant:
    """Parse one invariant from a YAML string or file path.

    Accepts either a path (loads the file) or a YAML document body.

    Raises:
        InvariantParseError: For any parse / shape failure.
    """
    if isinstance(text, Path):
        try:
            raw = text.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"Could not read invariant file {text}: {exc}"
            raise InvariantParseError(msg) from exc
    else:
        raw = text
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        msg = f"YAML parse error: {exc}"
        raise InvariantParseError(msg) from exc
    if data is None:
        msg = "Invariant document is empty."
        raise InvariantParseError(msg)
    return load_invariant_from_dict(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pattern(data: Any) -> PatternSpec:
    if not isinstance(data, dict):
        msg = f"Pattern must be a mapping, got {type(data).__name__}."
        raise InvariantParseError(msg)
    try:
        fact_type = str(data["fact_type"]).strip()
    except KeyError as exc:
        msg = "Pattern is missing 'fact_type'."
        raise InvariantParseError(msg) from exc
    if not fact_type:
        msg = "Pattern 'fact_type' must be non-empty."
        raise InvariantParseError(msg)
    raw_preds = data.get("predicates", ()) or ()
    if not isinstance(raw_preds, (list, tuple)):
        msg = "Pattern 'predicates' must be a list."
        raise InvariantParseError(msg)
    preds: list[PredicateSpec] = []
    for entry in raw_preds:
        preds.append(_parse_predicate(entry))
    return PatternSpec(fact_type=fact_type, predicates=tuple(preds))


def _parse_predicate(data: Any) -> PredicateSpec:
    if not isinstance(data, dict):
        msg = f"Predicate must be a mapping, got {type(data).__name__}."
        raise InvariantParseError(msg)
    try:
        field = str(data["field"]).strip()
        op_raw = str(data["op"]).strip().lower()
    except KeyError as exc:
        msg = f"Predicate is missing required key: {exc!s}."
        raise InvariantParseError(msg) from exc
    try:
        op = PredicateOp(op_raw)
    except ValueError as exc:
        valid = ", ".join(o.value for o in PredicateOp)
        msg = f"Unknown predicate op {op_raw!r}. Valid: {valid}."
        raise InvariantParseError(msg) from exc
    value = data.get("value")
    return PredicateSpec(field=field, op=op, value=value)


__all__ = [
    "Invariant",
    "InvariantParseError",
    "PatternSpec",
    "PredicateSpec",
    "load_invariant_from_dict",
    "load_invariant_from_yaml",
]
