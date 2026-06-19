"""CausalLedger — append-only event log + parent linkage (trace-mind M1).

The ledger is the substrate. Everything else (middleware, controller,
renderer) reads or writes through this module. It deliberately knows
nothing about LangChain or the TUI so it stays unit-testable without
either.

Design notes
------------

* **Append-only on disk.** Each event is one JSON line under
  ``<cwd>/.bog-agents/causal/<session_id>.jsonl``. We never rewrite
  the file — replay tools depend on the ordering being stable.
* **Monotonic event ids.** Each session resets the counter to 1. We
  never reuse an id within a session; parent_ids reference earlier
  ids in the same session.
* **Lazy fsync.** The user-visible cost has to stay tiny — we sync
  the directory on session open and rely on the OS to flush per-line
  writes. A crash loses at most the last few events; for trace-mind
  that is an acceptable tradeoff against per-turn latency.
* **No locking inside one session.** A session belongs to one
  conversation and is written from one task. If we ever support
  multi-session interleaving we add a per-file lock; today the
  cheapest correct answer is "don't."
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)


class EventKind(StrEnum):
    """The vocabulary of causal events.

    Kept deliberately small. Anything that doesn't fit cleanly should
    be carried in :attr:`CausalEvent.payload` rather than expand this
    enum — too many kinds turns the renderer into a switchboard.
    """

    USER_MESSAGE = "user_message"
    """Human turn — the seed of every downstream causal chain."""

    MODEL_CALL = "model_call"
    """One model invocation. Payload carries model id + token counts."""

    TOOL_CALL = "tool_call"
    """The model asked a tool to run. Payload has tool name + args."""

    TOOL_RESULT = "tool_result"
    """The tool returned. Payload has truncated result preview."""

    RULE_FIRE = "rule_fire"
    """Expert-rules engine fired a rule. Payload names the rule + action."""

    FACT_ASSERT = "fact_assert"
    """A fact was asserted into working memory."""

    DREAM_COMPLETE = "dream_complete"
    """A dream cycle finished. Payload has the dream title."""

    FINAL_ANSWER = "final_answer"
    """The model produced a user-facing answer (no further tool calls)."""

    NOTE = "note"
    """Free-form annotation. Useful for tests + manual debugging."""


_SCHEMA_VERSION = 1
"""Bumped only when the on-disk JSON shape changes incompatibly."""


@dataclass(frozen=True, slots=True)
class CausalEvent:
    """One record in the causal log.

    The frozen/slot'd combo keeps these cheap to construct in a hot
    path (one per tool call). Parent ids are tuples so equality stays
    well-defined; they are *direct* causal antecedents only — the
    full ancestry is reconstructed by walking parents transitively at
    read time.
    """

    id: int
    kind: EventKind
    timestamp: float
    actor: str
    summary: str
    parent_ids: tuple[int, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize as a single JSON line (no trailing newline)."""
        d = asdict(self)
        d["kind"] = self.kind.value
        return json.dumps(d, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CausalEvent:
        """Deserialize from a previously stored row."""
        return cls(
            id=int(data["id"]),
            kind=EventKind(data["kind"]),
            timestamp=float(data["timestamp"]),
            actor=str(data.get("actor", "")),
            summary=str(data.get("summary", "")),
            parent_ids=tuple(int(p) for p in data.get("parent_ids", ())),
            payload=dict(data.get("payload") or {}),
        )


def _session_root(working_dir: Path) -> Path:
    return working_dir / ".bog-agents" / "causal"


def _session_path(working_dir: Path, session_id: str) -> Path:
    return _session_root(working_dir) / f"{session_id}.jsonl"


def _new_session_id() -> str:
    """Return a sortable session id (UTC timestamp + short suffix)."""
    import uuid

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{ts}-{uuid.uuid4().hex[:6]}"


class CausalLedger:
    """Append-only event log for one agent session.

    Constructed via :func:`open_session`. Hold a reference for the
    lifetime of the session and call :meth:`record` per event;
    :meth:`close` flushes any pending state.
    """

    def __init__(
        self,
        *,
        working_dir: Path,
        session_id: str,
        existing_events: list[CausalEvent] | None = None,
    ) -> None:
        self._working_dir = working_dir
        self._session_id = session_id
        self._path = _session_path(working_dir, session_id)
        # The id counter is monotonic. When resuming an existing file
        # we continue from max(id) + 1 so re-opens keep working.
        self._events: list[CausalEvent] = list(existing_events or [])
        self._next_id: int = max((e.id for e in self._events), default=0) + 1
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------
    # Identity / iteration
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> list[CausalEvent]:
        """Return a shallow copy of every recorded event."""
        with self._lock:
            return list(self._events)

    def get(self, event_id: int) -> CausalEvent | None:
        """Return the event with id ``event_id`` or ``None``."""
        with self._lock:
            for e in self._events:
                if e.id == event_id:
                    return e
        return None

    def last(self, kind: EventKind | None = None) -> CausalEvent | None:
        """Return the most recent event, optionally filtered by kind."""
        with self._lock:
            for e in reversed(self._events):
                if kind is None or e.kind == kind:
                    return e
        return None

    # ------------------------------------------------------------------
    # Append
    # ------------------------------------------------------------------

    def record(
        self,
        kind: EventKind,
        *,
        actor: str,
        summary: str,
        parent_ids: tuple[int, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> CausalEvent:
        """Append one event and flush it to disk.

        Args:
            kind: Which :class:`EventKind` this is.
            actor: Short label naming who produced the event (model
                id, tool name, rule name, etc.). Used by the renderer
                as a column.
            summary: One-line human description. Long strings are
                truncated to 240 chars on the way to disk so the log
                stays grep-friendly.
            parent_ids: Direct causal antecedents (other event ids in
                this session). Empty for the root user_message.
            payload: Structured side-data. Must be JSON-serialisable.

        Returns:
            The recorded :class:`CausalEvent` (with its assigned id).

        Raises:
            RuntimeError: When called after :meth:`close`.
        """
        if self._closed:
            msg = "Ledger is closed — re-open the session before recording."
            raise RuntimeError(msg)
        clean_summary = (summary or "").strip().replace("\n", " ")
        if len(clean_summary) > 240:
            clean_summary = clean_summary[:239] + "…"
        with self._lock:
            event = CausalEvent(
                id=self._next_id,
                kind=kind,
                timestamp=time.time(),
                actor=actor,
                summary=clean_summary,
                parent_ids=tuple(int(p) for p in parent_ids),
                payload=dict(payload or {}),
            )
            self._next_id += 1
            self._events.append(event)
            self._append_to_disk(event)
        return event

    # ------------------------------------------------------------------
    # Graph queries
    # ------------------------------------------------------------------

    def ancestry(self, event_id: int, *, max_depth: int = 50) -> list[CausalEvent]:
        """Return every ancestor of ``event_id`` in BFS order.

        Args:
            event_id: Root of the walk.
            max_depth: Hard cap on traversal depth — guards against
                pathological cycles even though the ledger's append-
                only contract prevents them in normal use.
        """
        with self._lock:
            by_id = {e.id: e for e in self._events}
        root = by_id.get(event_id)
        if root is None:
            return []
        seen: set[int] = set()
        order: list[CausalEvent] = []
        frontier: list[tuple[int, int]] = [(event_id, 0)]
        while frontier:
            cur_id, depth = frontier.pop(0)
            if cur_id in seen or depth > max_depth:
                continue
            seen.add(cur_id)
            cur = by_id.get(cur_id)
            if cur is None:
                continue
            order.append(cur)
            for parent in cur.parent_ids:
                frontier.append((parent, depth + 1))
        return order

    def counts_by_kind(self) -> dict[EventKind, int]:
        """Histogram of event kinds in this session."""
        out: dict[EventKind, int] = dict.fromkeys(EventKind, 0)
        with self._lock:
            for e in self._events:
                out[e.kind] = out.get(e.kind, 0) + 1
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Mark the ledger closed. Future :meth:`record` calls raise."""
        self._closed = True

    # ------------------------------------------------------------------
    # Disk IO
    # ------------------------------------------------------------------

    def _append_to_disk(self, event: CausalEvent) -> None:
        """Append one JSON line. Caller holds ``self._lock``.

        We intentionally use plain ``open(..., "a")`` rather than an
        atomic-write helper — atomic writes for a 200-byte append
        would dominate per-turn latency. The session file is recovery-
        safe by virtue of being append-only; a torn final line is
        skipped on load.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(event.to_json())
                fh.write("\n")
        except OSError:
            logger.exception(
                "causal: failed to append event %d to %s",
                event.id,
                self._path,
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def open_session(
    working_dir: Path | str,
    *,
    session_id: str | None = None,
    resume: bool = False,
) -> CausalLedger:
    """Open a new (or existing) ledger for ``working_dir``.

    Args:
        working_dir: Project root.
        session_id: When supplied, use this exact id. When ``None``,
            a fresh sortable id is minted.
        resume: When True and the session file exists, load its
            contents so further ``record`` calls extend the same
            session. Default False: a fresh session is created and
            any existing file with the same id is left alone (you'll
            see a ValueError if it would be clobbered).
    """
    root = Path(working_dir)
    if session_id is None:
        session_id = _new_session_id()
    path = _session_path(root, session_id)
    if resume and path.exists():
        events = load_session(working_dir, session_id)
        return CausalLedger(
            working_dir=root, session_id=session_id, existing_events=events
        )
    if path.exists() and not resume:
        msg = (
            f"Causal session {session_id} already exists at {path}. "
            "Pass resume=True to continue it, or use a different id."
        )
        raise ValueError(msg)
    # Pre-create an empty file so the first append doesn't race with a
    # concurrent reader looking for the session.
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        atomic_write_text(path, "", encoding="utf-8")
    return CausalLedger(working_dir=root, session_id=session_id)


def load_session(
    working_dir: Path | str,
    session_id: str,
) -> list[CausalEvent]:
    """Return every :class:`CausalEvent` recorded for ``session_id``.

    Tolerant of a torn final line — incomplete JSON at the tail of the
    file is skipped (it's almost certainly the result of a crash mid-
    append; the data above it is still valid).
    """
    path = _session_path(Path(working_dir), session_id)
    if not path.exists():
        return []
    events: list[CausalEvent] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("causal: failed to read session %s", session_id)
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            # Torn final line — stop reading.
            logger.debug("causal: skipping unparseable line in %s", path.name)
            continue
        try:
            events.append(CausalEvent.from_dict(data))
        except (KeyError, ValueError, TypeError):
            logger.debug("causal: skipping malformed event in %s", path.name)
    return events


def list_sessions(working_dir: Path | str) -> list[str]:
    """Return session ids on disk for ``working_dir``, newest first."""
    root = _session_root(Path(working_dir))
    if not root.exists():
        return []
    sessions = sorted(
        (p.stem for p in root.glob("*.jsonl")),
        reverse=True,
    )
    return sessions


_SCHEMA_VERSION_HINT = _SCHEMA_VERSION  # silence "unused" lint


__all__ = [
    "CausalEvent",
    "CausalLedger",
    "EventKind",
    "list_sessions",
    "load_session",
    "open_session",
]
