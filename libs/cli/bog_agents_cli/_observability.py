"""Structured-logging + in-process metrics emitter.

Two surfaces, one module:

1. **Structured logs.** :func:`log_event` emits a single ``logger.info``
   with a stable event name and a flat ``extra`` dict. Code that
   ingests bog-agents logs (tail-by-grep, splunk, loki) can lock onto
   these event names. Examples:

   ::

       log_event("agent.run.start", model="claude-sonnet-4-6", thread_id="...")
       log_event("agent.run.end", duration_ms=4123, status="ok", tool_calls=7)
       log_event("peat.job.fire", job_id="morning-brief", run_id="run-...")
       log_event("vault.read", key="api_token", source="memory|keyring")
       log_event("mcp.load", server="jira", tool_count=12)
       log_event("provider.call", provider="anthropic", model="...", latency_ms=843, status="ok")

2. **In-process counters.** Each ``log_event`` call also bumps a
   counter keyed by event name (and optionally a label dimension) in a
   process-local registry. ``/peat metrics`` reads this to render a
   one-page health summary. The registry is intentionally small —
   counters only, no histograms — so it can never grow unbounded.

The module is deliberately *zero-dependency*: it uses stdlib logging,
no OpenTelemetry, no Prometheus client. We can layer those on top if
users need them; the data is already structured.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Any, Self

logger = logging.getLogger("bog_agents_cli.events")


# ---------------------------------------------------------------------------
# Counter registry
# ---------------------------------------------------------------------------


class _Registry:
    """Thread-safe counter registry.

    Counters are stored as ``{event_name: {label_value: count}}``.
    Events without labels live under the empty-string key so the shape
    is uniform for ``/peat metrics`` rendering.
    """

    # Bound on the cardinality of label values per event. Prevents a
    # malicious or buggy emitter from creating an unbounded number of
    # entries (e.g. emitting one label per uuid).
    _MAX_LABELS_PER_EVENT = 256

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._first_seen: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    def bump(self, event: str, label: str = "") -> None:
        now = time.time()
        with self._lock:
            event_map = self._counters[event]
            if label not in event_map and len(event_map) >= self._MAX_LABELS_PER_EVENT:
                # Cardinality cap hit. Bucket overflow under "*overflow*"
                # so the count is still recorded somewhere.
                label = "*overflow*"
            event_map[label] += 1
            if event not in self._first_seen:
                self._first_seen[event] = now
            self._last_seen[event] = now

    def snapshot(self) -> dict[str, Any]:
        """Return a stable, JSON-serialisable view of the registry."""
        with self._lock:
            return {
                "counters": {
                    event: dict(labels) for event, labels in self._counters.items()
                },
                "first_seen": dict(self._first_seen),
                "last_seen": dict(self._last_seen),
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._first_seen.clear()
            self._last_seen.clear()


_REGISTRY = _Registry()


def get_metrics_snapshot() -> dict[str, Any]:
    """Return a snapshot of all counters seen this process. Used by /peat metrics."""
    return _REGISTRY.snapshot()


def reset_metrics() -> None:
    """Test helper: drop the registry between tests."""
    _REGISTRY.reset()


# ---------------------------------------------------------------------------
# Event emitter
# ---------------------------------------------------------------------------


# Event names. Keep these short, dotted, and lowercase. Group by subsystem.
# This is the canonical list — emitters reference these constants so
# typos surface at import time rather than at log-grep time.

EVT_AGENT_RUN_START = "agent.run.start"
EVT_AGENT_RUN_END = "agent.run.end"
EVT_TOOL_DISPATCH = "tool.dispatch"
EVT_TOOL_RESULT = "tool.result"

EVT_PEAT_JOB_FIRE = "peat.job.fire"
EVT_PEAT_JOB_END = "peat.job.end"
EVT_PEAT_INBOX_APPEND = "peat.inbox.append"
EVT_PEAT_SCHEDULER_TICK = "peat.scheduler.tick"

EVT_VAULT_READ = "vault.read"
EVT_VAULT_WRITE = "vault.write"

EVT_MCP_LOAD = "mcp.load"
EVT_MCP_CALL = "mcp.call"

EVT_PROVIDER_CALL = "provider.call"
EVT_PROVIDER_RETRY = "provider.retry"

EVT_QA_RUN_START = "qa.run.start"
EVT_QA_RUN_END = "qa.run.end"
EVT_QA_STEP = "qa.step"

EVT_REPLAY_RECORD_START = "replay.record.start"
EVT_REPLAY_RECORD_STOP = "replay.record.stop"
EVT_REPLAY_RUN = "replay.run"


def log_event(
    event: str, *, label: str = "", level: int = logging.INFO, **fields: Any
) -> None:
    """Emit a structured event.

    Args:
        event: Stable event name (use one of the ``EVT_*`` constants).
        label: Optional label dimension for counter aggregation. Keep
            cardinality low — model id is fine, full thread_id is not.
        level: Log level. Defaults to INFO.
        **fields: Free-form structured fields surfaced as ``extra``.
    """
    _REGISTRY.bump(event, label)
    # ``extra`` keys must not collide with LogRecord built-ins. Prefix
    # everything with "evt_" defensively.
    safe_fields = {f"evt_{k}": v for k, v in fields.items()}
    safe_fields["evt_event"] = event
    if label:
        safe_fields["evt_label"] = label
    logger.log(level, "%s %s", event, _format_fields(fields), extra=safe_fields)


def _format_fields(fields: dict[str, Any]) -> str:
    """Render fields as a one-line ``k=v`` string for human-readable log lines."""
    if not fields:
        return ""
    parts: list[str] = []
    for k, v in fields.items():
        # Avoid logging large objects; cap each value's repr to 80 chars.
        rendered = (
            repr(v)
            if not isinstance(v, (str, int, float, bool, type(None)))
            else str(v)
        )
        if len(rendered) > 80:
            rendered = rendered[:77] + "..."
        parts.append(f"{k}={rendered}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Convenience timer
# ---------------------------------------------------------------------------


class timer:  # noqa: N801 — lowercase is intentional, used as a context manager
    """Context manager that emits a duration_ms field on exit.

    Example::

        with timer("agent.run") as t:
            do_work()
            t.fields["status"] = "ok"
        # Emits: agent.run.end status=ok duration_ms=...

    On exception, ``status`` defaults to ``"error"`` and the exception
    class name is logged.
    """

    __slots__ = ("_start", "event", "fields", "level")

    def __init__(self, event_prefix: str, *, level: int = logging.INFO) -> None:
        self.event = event_prefix + ".end"
        self.fields: dict[str, Any] = {}
        self.level = level
        self._start: float = 0.0

    def __enter__(self) -> Self:
        self._start = time.monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: object,
    ) -> None:
        elapsed_ms = int((time.monotonic() - self._start) * 1000)
        if exc_type is not None and "status" not in self.fields:
            self.fields["status"] = "error"
            self.fields["error_type"] = exc_type.__name__
        elif "status" not in self.fields:
            self.fields["status"] = "ok"
        self.fields["duration_ms"] = elapsed_ms
        log_event(self.event, level=self.level, **self.fields)
