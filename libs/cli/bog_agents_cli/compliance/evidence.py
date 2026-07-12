"""Evidence collectors over causal traces (Wave R, R2).

These helpers walk the causal-trace log under ``<cwd>/.bog-agents/causal/``
and answer the questions audit-pack ``trace_assertion`` checks pose:

* "Did the agent record at least N events of kind K in the window?"
* "Did *no* rule_fire with payload action=deny fire (optionally on
  actor=X) in the window?" (``no_event_with_payload`` — the verdict
  lives in ``payload.action``, so plain actor matching can't see it.)
* "Is there at least one session at all?"

The collectors are pure — they read from disk and return concrete
:class:`Evidence` records. The runner converts evidence into a
PASS / FAIL verdict.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    EventKind,
    list_sessions,
    load_session,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    """Time window the collector reads from.

    Attributes:
        oldest_allowed: Epoch seconds. Events with ``timestamp``
            strictly less than this are excluded.
        now: Epoch seconds at the time the audit started. Recorded
            so the report can show "window = [a, b]".
    """

    oldest_allowed: float
    now: float


def window_for_lookback(
    lookback_hours: float, *, now: float | None = None
) -> EvidenceWindow:
    """Build an :class:`EvidenceWindow` from a lookback in hours."""
    current = time.time() if now is None else now
    return EvidenceWindow(
        oldest_allowed=current - (lookback_hours * 3600.0),
        now=current,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceSlice:
    """Materialised subset of the causal log inside an :class:`EvidenceWindow`.

    Loaded once per audit and reused by every collector — this keeps
    the audit's IO cost proportional to the log size, not to the
    number of checks.
    """

    window: EvidenceWindow
    sessions: tuple[str, ...]
    events: tuple[CausalEvent, ...]


def load_trace_slice(working_dir: Path, window: EvidenceWindow) -> TraceSlice:
    """Read every session under *working_dir* and filter to the window."""
    sessions = list_sessions(working_dir)
    all_events: list[CausalEvent] = []
    kept_sessions: list[str] = []
    for sid in sessions:
        try:
            events = load_session(working_dir, sid)
        except Exception:
            logger.warning(
                "compliance: could not load session %s; skipping",
                sid,
                exc_info=True,
            )
            continue
        in_window = [e for e in events if e.timestamp >= window.oldest_allowed]
        if in_window:
            kept_sessions.append(sid)
            all_events.extend(in_window)
    return TraceSlice(
        window=window,
        sessions=tuple(kept_sessions),
        events=tuple(all_events),
    )


@dataclass(frozen=True, slots=True)
class EvidenceFinding:
    """Concrete output of one collector.

    Attributes:
        passes: Whether the assertion is satisfied.
        observed: Description of what was measured ("3 user_message
            events"; "0 deny rule-fires"). Surfaced in the report.
        samples: Up to N example events that *would have* changed
            the verdict — passing checks include positive examples,
            failing checks include the offending events.
    """

    passes: bool
    observed: str
    samples: tuple[CausalEvent, ...] = ()
    inconclusive: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def collect_event_count(slice_: TraceSlice, params: dict[str, Any]) -> EvidenceFinding:
    """``trace_assertion`` with ``kind: event_count``.

    Params:
        fact_kind: required EventKind value
            (``user_message``, ``rule_fire``, etc.).
        min: optional integer lower bound (inclusive).
        max: optional integer upper bound (inclusive).
    """
    fact_kind = _required_event_kind(params, "fact_kind")
    if fact_kind is None:
        return EvidenceFinding(
            passes=False,
            observed="",
            inconclusive=True,
            reason="evidence.fact_kind is required for event_count checks",
        )
    matching = [e for e in slice_.events if e.kind == fact_kind]
    count = len(matching)
    min_bound, max_bound, reason = _parse_bounds(params)
    if reason:
        return EvidenceFinding(
            passes=False, observed="", inconclusive=True, reason=reason
        )
    ok = True
    if min_bound is not None and count < min_bound:
        ok = False
    if max_bound is not None and count > max_bound:
        ok = False
    bounds_text = _format_bounds(min_bound, max_bound)
    observed = (
        f"{count} event(s) of kind {fact_kind.value!r} "
        f"in the audit window (bounds: {bounds_text})"
    )
    samples = tuple(matching[:3]) if matching else ()
    return EvidenceFinding(passes=ok, observed=observed, samples=samples)


def collect_no_event_with_actor(
    slice_: TraceSlice, params: dict[str, Any]
) -> EvidenceFinding:
    """``trace_assertion`` with ``kind: no_event_with_actor``.

    Params:
        fact_kind: required EventKind value.
        actor: required string. The actor field on the matching events.
    """
    fact_kind = _required_event_kind(params, "fact_kind")
    if fact_kind is None:
        return EvidenceFinding(
            passes=False,
            observed="",
            inconclusive=True,
            reason="evidence.fact_kind is required for no_event_with_actor",
        )
    actor = params.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        return EvidenceFinding(
            passes=False,
            observed="",
            inconclusive=True,
            reason="evidence.actor is required and must be a non-empty string",
        )
    actor_clean = actor.strip()
    matches = [
        e for e in slice_.events if e.kind == fact_kind and e.actor == actor_clean
    ]
    observed = (
        f"{len(matches)} event(s) of kind {fact_kind.value!r} "
        f"with actor={actor_clean!r} in the audit window "
        "(expected: 0)"
    )
    return EvidenceFinding(
        passes=len(matches) == 0,
        observed=observed,
        samples=tuple(matches[:3]),
    )


def collect_no_event_with_payload(
    slice_: TraceSlice, params: dict[str, Any]
) -> EvidenceFinding:
    """``trace_assertion`` with ``kind: no_event_with_payload``.

    Asserts that *no* event of the given kind carries the supplied
    payload key/value pairs. This is the collector that can actually
    inspect the rule *verdict* — ``rule_fire`` events record the
    action (``deny`` / ``require_approval`` / ``modify``) under
    ``payload.action``, so a pack can assert e.g. "no deny-control
    rule fired" with ``fact_kind: rule_fire`` and
    ``payload_match: {action: deny}``.

    Params:
        fact_kind: required EventKind value.
        actor: optional string. When set, narrows the match to events
            whose ``actor`` equals this value (e.g. a specific rule
            name). When omitted, every actor is considered.
        payload_match: required non-empty mapping. An event matches
            only when, for every key, the event's ``payload`` contains
            that key with an equal value.
    """
    fact_kind = _required_event_kind(params, "fact_kind")
    if fact_kind is None:
        return EvidenceFinding(
            passes=False,
            observed="",
            inconclusive=True,
            reason="evidence.fact_kind is required for no_event_with_payload",
        )
    payload_match = params.get("payload_match")
    if not isinstance(payload_match, dict) or not payload_match:
        return EvidenceFinding(
            passes=False,
            observed="",
            inconclusive=True,
            reason="evidence.payload_match is required and must be a non-empty mapping",
        )
    actor_raw = params.get("actor")
    actor_clean: str | None = None
    if actor_raw is not None:
        if not isinstance(actor_raw, str) or not actor_raw.strip():
            return EvidenceFinding(
                passes=False,
                observed="",
                inconclusive=True,
                reason="evidence.actor, when set, must be a non-empty string",
            )
        actor_clean = actor_raw.strip()
    matches = [
        e
        for e in slice_.events
        if e.kind == fact_kind
        and (actor_clean is None or e.actor == actor_clean)
        and _payload_matches(e.payload, payload_match)
    ]
    actor_text = f"actor={actor_clean!r}, " if actor_clean is not None else ""
    observed = (
        f"{len(matches)} event(s) of kind {fact_kind.value!r} "
        f"with {actor_text}payload matching {payload_match!r} "
        "in the audit window (expected: 0)"
    )
    return EvidenceFinding(
        passes=len(matches) == 0,
        observed=observed,
        samples=tuple(matches[:3]),
    )


def collect_at_least_one_session(
    slice_: TraceSlice, params: dict[str, Any]
) -> EvidenceFinding:
    """``trace_assertion`` with ``kind: at_least_one_session``.

    No params. Passes when the window contains at least one recorded
    session — a sanity check that observability is *actually on*.
    """
    _ = params  # unused
    count = len(slice_.sessions)
    return EvidenceFinding(
        passes=count >= 1,
        observed=f"{count} session(s) recorded in the audit window",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _required_event_kind(params: dict[str, Any], key: str) -> EventKind | None:
    raw = params.get(key)
    if not isinstance(raw, str):
        return None
    try:
        return EventKind(raw)
    except ValueError:
        return None


def _parse_bounds(
    params: dict[str, Any],
) -> tuple[int | None, int | None, str]:
    """Return ``(min, max, reason)``. ``reason`` non-empty on parse error."""
    min_bound: int | None = None
    max_bound: int | None = None
    if "min" in params:
        try:
            min_bound = int(params["min"])
        except (ValueError, TypeError):
            return None, None, f"evidence.min must be an integer, got {params['min']!r}"
    if "max" in params:
        try:
            max_bound = int(params["max"])
        except (ValueError, TypeError):
            return None, None, f"evidence.max must be an integer, got {params['max']!r}"
    if min_bound is not None and max_bound is not None and min_bound > max_bound:
        return (
            None,
            None,
            f"evidence.min ({min_bound}) cannot exceed evidence.max ({max_bound})",
        )
    return min_bound, max_bound, ""


def _payload_matches(payload: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Return True when *payload* contains every key/value in *expected*.

    A subset match: extra keys on the event payload are ignored. The
    comparison is exact equality per key, so ``{"action": "deny"}``
    matches a payload of ``{"action": "deny", "detail": "..."}`` but
    not ``{"action": "require_approval"}``.
    """
    for key, value in expected.items():
        if key not in payload or payload[key] != value:
            return False
    return True


def _format_bounds(min_bound: int | None, max_bound: int | None) -> str:
    if min_bound is None and max_bound is None:
        return "any"
    if min_bound is None:
        return f"≤ {max_bound}"
    if max_bound is None:
        return f"≥ {min_bound}"
    return f"{min_bound}..{max_bound}"


# Public dispatch table — keeps the runner thin.
COLLECTORS = {
    "event_count": collect_event_count,
    "no_event_with_actor": collect_no_event_with_actor,
    "no_event_with_payload": collect_no_event_with_payload,
    "at_least_one_session": collect_at_least_one_session,
}


__all__ = [
    "COLLECTORS",
    "EvidenceFinding",
    "EvidenceWindow",
    "TraceSlice",
    "collect_at_least_one_session",
    "collect_event_count",
    "collect_no_event_with_actor",
    "collect_no_event_with_payload",
    "load_trace_slice",
    "window_for_lookback",
]
