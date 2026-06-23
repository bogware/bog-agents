"""Audit-pack data model + YAML loader (Wave R, R1).

An *audit pack* is a versioned YAML document listing **checks** the
auditor will run. Each check has a kind:

* ``invariant`` — wraps a Q1 invariant. PASS when
  :func:`bog_agents_cli.policy_prove.prove` returns HOLDS;
  FAIL on COUNTEREXAMPLE; INCONCLUSIVE otherwise.
* ``trace_assertion`` — declarative claim about the causal trace
  log within the audit window (e.g. "at least 1 user_message", or
  "no rule_fire with payload action=deny" via the
  ``no_event_with_payload`` evidence kind — the verdict lives in
  ``payload.action``, so a plain actor match can't assert on it).
* ``rule_presence`` — assert that a named rule is loaded (used to
  detect rule-pack drift / accidental disable).
* ``rule_absence`` — assert that no rule with the given name exists
  (used to gate out forbidden policies).

YAML schema (illustrative)::

    version: 1
    name: soc2-baseline
    description: Baseline SOC2 controls.
    window:
      lookback_hours: 24
    checks:
      - id: CC6.1
        title: Logical access — shell calls require approval
        kind: invariant
        control: SOC2.CC6.1
        invariant:
          name: shell_requires_approval
          precondition: {fact_type: tool_call, predicates: [{field: name, op: eq, value: shell_execute}]}
          forbidden:    {fact_type: tool_call, predicates: [{field: requires_approval, op: eq, value: false}]}
      - id: CC7.2-a
        title: At least one trace recorded in the window
        kind: trace_assertion
        control: SOC2.CC7.2
        evidence:
          kind: event_count
          fact_kind: user_message
          min: 1
      - id: CC8.1
        title: The force-push rule must be loaded
        kind: rule_presence
        control: SOC2.CC8.1
        rule_name: block_force_push_to_main

The loader is strict — unknown keys raise :class:`PackParseError`
rather than silently ignore. Versioning is explicit so the loader
can refuse a pack written against a future schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from bog_agents_cli.policy_prove.invariant import (
    Invariant,
    InvariantParseError,
    load_invariant_from_dict,
)

_SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


class PackParseError(ValueError):
    """Raised when an audit pack YAML/dict can't be parsed cleanly."""


class CheckKind(StrEnum):
    """The vocabulary of audit checks."""

    INVARIANT = "invariant"
    TRACE_ASSERTION = "trace_assertion"
    RULE_PRESENCE = "rule_presence"
    RULE_ABSENCE = "rule_absence"


class EvidenceKind(StrEnum):
    """Sub-vocabulary for ``trace_assertion`` checks."""

    EVENT_COUNT = "event_count"
    """Count events of a given kind within the window.

    Parameters:
        ``fact_kind`` — required :class:`~bog_agents_cli.causal.EventKind` value.
        ``min`` / ``max`` — optional integer bounds (inclusive). Pass passes
            when count satisfies both bounds.
    """

    NO_EVENT_WITH_ACTOR = "no_event_with_actor"
    """Assert no event with ``kind=<kind>`` and ``actor=<actor>`` fires
    within the window. Used for "this rule must never fire" claims.
    """

    NO_EVENT_WITH_PAYLOAD = "no_event_with_payload"
    """Assert no event with ``kind=<kind>`` carries the given
    ``payload_match`` key/values (optionally narrowed by ``actor``).

    This is the only collector that inspects the rule *verdict*:
    ``rule_fire`` events record the decision under ``payload.action``,
    so "no deny-control rule fired" is expressible as
    ``fact_kind: rule_fire`` + ``payload_match: {action: deny}``.
    """

    AT_LEAST_ONE_SESSION = "at_least_one_session"
    """The trace log must contain at least one session in the window."""


@dataclass(frozen=True, slots=True)
class EvidenceSpec:
    """Parameters for a ``trace_assertion`` check.

    Attributes:
        kind: Which evidence collector to run.
        params: Free-form parameter dict (each collector validates
            its own keys).
    """

    kind: EvidenceKind
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Check:
    """One audit check.

    Attributes:
        id: Stable identifier the report references (e.g. ``CC6.1``).
            The pack's authors choose this; we don't validate
            against any specific framework.
        title: Human-readable summary.
        kind: Which check kind to dispatch to at run time.
        control: Optional cross-framework control id
            (``SOC2.CC6.1``, ``ISO27001.A.9.1.1``, etc.). Surfaced
            in the report's "control coverage" table.
        invariant: When :attr:`kind` is ``invariant``, the invariant
            to prove. Otherwise ``None``.
        evidence: When :attr:`kind` is ``trace_assertion``, the
            evidence spec. Otherwise ``None``.
        rule_name: When :attr:`kind` is ``rule_presence`` or
            ``rule_absence``, the rule name. Otherwise empty.
        description: Optional longer-form text shown in the report.
    """

    id: str
    title: str
    kind: CheckKind
    control: str = ""
    invariant: Invariant | None = None
    evidence: EvidenceSpec | None = None
    rule_name: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class AuditWindow:
    """Time window within which evidence is collected."""

    lookback_hours: float = 24.0
    """Hours of causal-trace history to include in evidence
    collection. Anything older than ``now - lookback_hours`` is
    excluded from the audit."""


@dataclass(frozen=True, slots=True)
class AuditPack:
    """A versioned audit pack — the unit the auditor runs."""

    name: str
    description: str
    version: int
    window: AuditWindow
    checks: tuple[Check, ...]
    source_path: Path | None = None
    """When loaded from a file, the original path. ``None`` for
    in-memory packs (tests + ad-hoc usage)."""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_pack_from_dict(data: dict[str, Any]) -> AuditPack:
    """Parse one :class:`AuditPack` from a Python dict.

    Raises:
        PackParseError: When the dict is malformed or the schema
            version isn't supported.
    """
    if not isinstance(data, dict):
        msg = f"AuditPack must be a mapping, got {type(data).__name__}."
        raise PackParseError(msg)
    try:
        version = int(data["version"])
    except KeyError as exc:
        msg = "AuditPack is missing 'version'."
        raise PackParseError(msg) from exc
    except (ValueError, TypeError) as exc:
        msg = f"AuditPack 'version' must be an integer, got {data['version']!r}."
        raise PackParseError(msg) from exc
    if version not in _SUPPORTED_SCHEMA_VERSIONS:
        msg = (
            f"AuditPack version {version} not supported. "
            f"Supported versions: {sorted(_SUPPORTED_SCHEMA_VERSIONS)}."
        )
        raise PackParseError(msg)

    try:
        name = str(data["name"]).strip()
    except KeyError as exc:
        msg = "AuditPack is missing 'name'."
        raise PackParseError(msg) from exc
    if not name:
        msg = "AuditPack 'name' must be non-empty."
        raise PackParseError(msg)
    description = str(data.get("description", "")).strip()

    window = _parse_window(data.get("window") or {})

    raw_checks = data.get("checks", [])
    if not isinstance(raw_checks, list):
        msg = "AuditPack 'checks' must be a list."
        raise PackParseError(msg)
    if not raw_checks:
        msg = "AuditPack has no checks — nothing to audit."
        raise PackParseError(msg)

    seen_ids: set[str] = set()
    checks: list[Check] = []
    for raw in raw_checks:
        check = _parse_check(raw)
        if check.id in seen_ids:
            msg = f"AuditPack has duplicate check id {check.id!r}."
            raise PackParseError(msg)
        seen_ids.add(check.id)
        checks.append(check)

    return AuditPack(
        name=name,
        description=description,
        version=version,
        window=window,
        checks=tuple(checks),
    )


def load_pack_from_yaml(text: str | Path) -> AuditPack:
    """Parse an :class:`AuditPack` from YAML text or a file path."""
    if isinstance(text, Path):
        try:
            raw = text.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"Could not read audit pack file {text}: {exc}"
            raise PackParseError(msg) from exc
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            msg = f"YAML parse error in {text}: {exc}"
            raise PackParseError(msg) from exc
        pack = load_pack_from_dict(parsed if parsed is not None else {})
        # Preserve the source path so the report renderer can show it.
        return AuditPack(
            name=pack.name,
            description=pack.description,
            version=pack.version,
            window=pack.window,
            checks=pack.checks,
            source_path=text,
        )
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"YAML parse error: {exc}"
        raise PackParseError(msg) from exc
    if parsed is None:
        msg = "Audit pack document is empty."
        raise PackParseError(msg)
    return load_pack_from_dict(parsed)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_window(data: Any) -> AuditWindow:
    if not isinstance(data, dict):
        msg = f"AuditPack 'window' must be a mapping, got {type(data).__name__}."
        raise PackParseError(msg)
    raw_lookback = data.get("lookback_hours", 24.0)
    try:
        lookback = float(raw_lookback)
    except (ValueError, TypeError) as exc:
        msg = (
            f"AuditPack 'window.lookback_hours' must be a number, got {raw_lookback!r}."
        )
        raise PackParseError(msg) from exc
    if lookback <= 0:
        msg = f"AuditPack 'window.lookback_hours' must be positive, got {lookback}."
        raise PackParseError(msg)
    return AuditWindow(lookback_hours=lookback)


def _parse_check(data: Any) -> Check:
    if not isinstance(data, dict):
        msg = f"Check must be a mapping, got {type(data).__name__}."
        raise PackParseError(msg)
    try:
        check_id = str(data["id"]).strip()
        title = str(data["title"]).strip()
        kind_raw = str(data["kind"]).strip().lower()
    except KeyError as exc:
        msg = f"Check is missing required key: {exc!s}."
        raise PackParseError(msg) from exc
    try:
        kind = CheckKind(kind_raw)
    except ValueError as exc:
        valid = ", ".join(k.value for k in CheckKind)
        msg = f"Unknown check kind {kind_raw!r}. Valid: {valid}."
        raise PackParseError(msg) from exc

    control = str(data.get("control", "")).strip()
    description = str(data.get("description", "")).strip()

    invariant: Invariant | None = None
    evidence: EvidenceSpec | None = None
    rule_name = ""

    if kind == CheckKind.INVARIANT:
        if "invariant" not in data:
            msg = f"Check {check_id!r}: kind=invariant requires an 'invariant' block."
            raise PackParseError(msg)
        try:
            invariant = load_invariant_from_dict(data["invariant"])
        except InvariantParseError as exc:
            msg = f"Check {check_id!r}: invariant parse error: {exc}"
            raise PackParseError(msg) from exc
    elif kind == CheckKind.TRACE_ASSERTION:
        if "evidence" not in data:
            msg = (
                f"Check {check_id!r}: kind=trace_assertion requires an "
                "'evidence' block."
            )
            raise PackParseError(msg)
        evidence = _parse_evidence(data["evidence"], check_id=check_id)
    elif kind in (CheckKind.RULE_PRESENCE, CheckKind.RULE_ABSENCE):
        try:
            rule_name = str(data["rule_name"]).strip()
        except KeyError as exc:
            msg = f"Check {check_id!r}: kind={kind.value} requires 'rule_name'."
            raise PackParseError(msg) from exc
        if not rule_name:
            msg = f"Check {check_id!r}: 'rule_name' must be non-empty."
            raise PackParseError(msg)

    return Check(
        id=check_id,
        title=title,
        kind=kind,
        control=control,
        invariant=invariant,
        evidence=evidence,
        rule_name=rule_name,
        description=description,
    )


def _parse_evidence(data: Any, *, check_id: str) -> EvidenceSpec:
    if not isinstance(data, dict):
        msg = f"Check {check_id!r}: evidence must be a mapping."
        raise PackParseError(msg)
    try:
        kind_raw = str(data["kind"]).strip().lower()
    except KeyError as exc:
        msg = f"Check {check_id!r}: evidence is missing 'kind'."
        raise PackParseError(msg) from exc
    try:
        kind = EvidenceKind(kind_raw)
    except ValueError as exc:
        valid = ", ".join(k.value for k in EvidenceKind)
        msg = f"Check {check_id!r}: unknown evidence kind {kind_raw!r}. Valid: {valid}."
        raise PackParseError(msg) from exc
    params = {k: v for k, v in data.items() if k != "kind"}
    return EvidenceSpec(kind=kind, params=params)


__all__ = [
    "AuditPack",
    "AuditWindow",
    "Check",
    "CheckKind",
    "EvidenceKind",
    "EvidenceSpec",
    "PackParseError",
    "load_pack_from_dict",
    "load_pack_from_yaml",
]
