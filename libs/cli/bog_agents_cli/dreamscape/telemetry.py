"""Event-level telemetry for dreamscape operations.

The campaign's effectiveness measurements (Phases 10-22) are all
*offline*: scripted scenarios, blind A/B with a Sonnet judge,
aggregated win rates. That's a good proxy for "does dreamscape help
on the prompts I expect it to help on" — but it can't answer "does
dreamscape help my real users on their real work?"

Phase 25 ships the infrastructure for the second question. Every
dream firing, every imagination-injection, every "did the injection
help" signal is logged to a per-agent JSONL file:

    ~/.bog-agents/agents/<agent_id>/telemetry.jsonl

Each line is one JSON object:

    {
      "ts": 1778670000.123,
      "kind": "dream_fired" | "injection_fired" | "injection_helped",
      "agent_id": "myagent",
      "metadata": { ... per-event fields ... }
    }

The events accumulate during normal use. A future research phase or
a deployment operator can run :func:`aggregate_events` over the log
to answer questions like:

* How many dreams fire per agent per day at production cadence?
* What's the injection-helped rate over the last 7 days?
* Which injection style (dreams vs neutral) fires more often?

This module never raises into the prompt path. Disk errors degrade
to "no telemetry recorded" rather than blocking the feature it's
observing.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

logger = logging.getLogger(__name__)


_FILE_NAME = "telemetry.jsonl"
_MAX_FILE_BYTES = 1_048_576  # 1 MB cap — rotate to .1 when exceeded
_VALID_KINDS = frozenset({"dream_fired", "injection_fired", "injection_helped"})


EventKind = Literal["dream_fired", "injection_fired", "injection_helped"]


@dataclass
class TelemetryEvent:
    """One recorded telemetry event."""

    timestamp: float
    """Unix epoch seconds when the event was recorded."""

    kind: EventKind
    """One of ``"dream_fired"``, ``"injection_fired"``, ``"injection_helped"``."""

    agent_id: str
    """Per-agent identifier the event belongs to."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Event-specific fields (dream title, injection style, etc.)."""

    @classmethod
    def from_line(cls, line: str) -> TelemetryEvent | None:
        """Parse one JSONL line. Returns None on malformed input."""
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        ts = obj.get("ts")
        kind = obj.get("kind")
        agent_id = obj.get("agent_id")
        metadata = obj.get("metadata") or {}
        if not isinstance(ts, (int, float)):
            return None
        if not isinstance(kind, str) or kind not in _VALID_KINDS:
            return None
        if not isinstance(agent_id, str):
            return None
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            timestamp=float(ts),
            kind=kind,  # type: ignore[arg-type]
            agent_id=agent_id,
            metadata=dict(metadata),
        )


def telemetry_path(agent_id: str) -> Path:
    """Return ``~/.bog-agents/agents/<agent_id>/telemetry.jsonl``."""
    return agent_state_dir(agent_id) / _FILE_NAME


def record_event(
    agent_id: str, kind: EventKind, metadata: dict[str, Any] | None = None
) -> bool:
    """Append one event to the per-agent telemetry log.

    Args:
        agent_id: Per-agent identifier.
        kind: One of the recognized event kinds.
        metadata: Optional event-specific fields.

    Returns:
        True on successful append, False on any error or invalid input.
        Never raises.
    """
    if kind not in _VALID_KINDS:
        return False
    path = telemetry_path(agent_id)
    entry = {
        "ts": time.time(),
        "kind": kind,
        "agent_id": agent_id,
        "metadata": dict(metadata or {}),
    }
    try:
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.warning("dreamscape telemetry write failed (%s): %s", path, exc)
        return False
    return True


def iter_events(
    agent_id: str, *, since: float | None = None, kind: EventKind | None = None
) -> Iterator[TelemetryEvent]:
    """Stream events from the per-agent telemetry log.

    Args:
        agent_id: Per-agent identifier.
        since: Optional Unix epoch lower bound. Events older than this
            are skipped. ``None`` means "all events."
        kind: Optional filter to only emit events of a specific kind.

    Yields:
        :class:`TelemetryEvent` records in file order (which is
        chronological because the file is append-only).
    """
    path = telemetry_path(agent_id)
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                event = TelemetryEvent.from_line(line)
                if event is None:
                    continue
                if since is not None and event.timestamp < since:
                    continue
                if kind is not None and event.kind != kind:
                    continue
                yield event
    except OSError as exc:
        logger.warning("dreamscape telemetry read failed (%s): %s", path, exc)


@dataclass
class TelemetryAggregate:
    """Aggregated view over a per-agent telemetry log.

    Returned by :func:`aggregate_events`. Captures the counts and
    rates an operator typically wants to see.
    """

    agent_id: str
    window_start: float | None
    window_end: float
    events_total: int = 0
    dreams_fired: int = 0
    injections_fired: int = 0
    injections_helped: int = 0
    dreams_by_category: dict[str, int] = field(default_factory=dict)
    injections_by_style: dict[str, int] = field(default_factory=dict)

    @property
    def helped_rate(self) -> float | None:
        """Fraction of injections followed by a non-error response.

        Returns ``None`` when there are no recorded injections (the
        rate is undefined).
        """
        if self.injections_fired == 0:
            return None
        return self.injections_helped / self.injections_fired

    @property
    def approx_cost_usd(self) -> float:
        """Rough cost estimate at $0.001 per dream (Haiku rate)."""
        return round(self.dreams_fired * 0.001, 4)


def aggregate_events(
    agent_id: str, *, since: float | None = None
) -> TelemetryAggregate:
    """Reduce the per-agent telemetry log into an aggregate view.

    Args:
        agent_id: Per-agent identifier.
        since: Optional Unix epoch lower bound. ``None`` means
            "the entire log."

    Returns:
        A :class:`TelemetryAggregate` with counts + rates.
    """
    agg = TelemetryAggregate(
        agent_id=agent_id,
        window_start=since,
        window_end=time.time(),
    )
    for event in iter_events(agent_id, since=since):
        agg.events_total += 1
        if event.kind == "dream_fired":
            agg.dreams_fired += 1
            cat = event.metadata.get("category")
            if isinstance(cat, str) and cat:
                agg.dreams_by_category[cat] = agg.dreams_by_category.get(cat, 0) + 1
        elif event.kind == "injection_fired":
            agg.injections_fired += 1
            style = event.metadata.get("injection_style")
            if isinstance(style, str) and style:
                agg.injections_by_style[style] = (
                    agg.injections_by_style.get(style, 0) + 1
                )
        elif event.kind == "injection_helped":
            agg.injections_helped += 1
    return agg


def _rotate_if_needed(path: Path) -> None:
    """Rotate the log to .1 when it grows past ``_MAX_FILE_BYTES``."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < _MAX_FILE_BYTES:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    with suppress(OSError):
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)


def clear_telemetry(agent_id: str) -> bool:
    """Delete the per-agent telemetry log. Used by tests + operators."""
    path = telemetry_path(agent_id)
    rotated = path.with_suffix(path.suffix + ".1")
    ok = True
    for p in (path, rotated):
        if p.exists():
            try:
                p.unlink()
            except OSError as exc:
                logger.warning("dreamscape telemetry clear failed (%s): %s", p, exc)
                ok = False
    return ok
