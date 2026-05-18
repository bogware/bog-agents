"""Text renderers for the causal log (M3).

The renderers take a :class:`~bog_agents_cli.causal.ledger.CausalLedger`
(or a list of :class:`CausalEvent`) and produce plain text suitable for
inline display in the TUI. There are three audiences:

* **Status summary** (``/causal``) — short, scannable, fits one screen.
* **Event list** (``/causal last [N]``) — append-order timeline.
* **Causal tree** (``/causal why <id>``) — proof-tree-style ancestry
  walk, indented by depth, with the requested event at the root.

We render in plain text first; Rich markup is applied lightly so the
output stays readable in transcripts and tests can assert on strings
without stripping markup.
"""

from __future__ import annotations

import time
from collections import deque

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    CausalLedger,
    EventKind,
)

_KIND_LABELS: dict[EventKind, str] = {
    EventKind.USER_MESSAGE: "USER",
    EventKind.MODEL_CALL: "MODEL",
    EventKind.TOOL_CALL: "TOOL >",
    EventKind.TOOL_RESULT: "TOOL <",
    EventKind.RULE_FIRE: "RULE",
    EventKind.FACT_ASSERT: "FACT",
    EventKind.DREAM_COMPLETE: "DREAM",
    EventKind.FINAL_ANSWER: "ANSWER",
    EventKind.NOTE: "NOTE",
}


def _format_event_line(event: CausalEvent, *, indent: int = 0) -> str:
    """Render one event as a single ASCII line."""
    prefix = "  " * indent
    label = _KIND_LABELS.get(event.kind, event.kind.value.upper())
    parents = (
        f" ← {','.join(str(p) for p in event.parent_ids)}" if event.parent_ids else ""
    )
    return (
        f"{prefix}#{event.id:>4}  [{label:<6}] {event.actor:<24} "
        f"{event.summary}{parents}"
    )


def render_status(ledger: CausalLedger) -> str:
    """Short status block for ``/causal`` with no subcommand."""
    counts = ledger.counts_by_kind()
    total = sum(counts.values())
    if total == 0:
        return (
            f"Causal session: {ledger.session_id}\n"
            f"No events recorded yet. Send a prompt and try again."
        )
    last = ledger.last()
    last_line = _format_event_line(last) if last else "(no events)"
    nonzero = sorted(
        ((k, v) for k, v in counts.items() if v > 0),
        key=lambda kv: -kv[1],
    )
    histogram = "  ".join(f"{_KIND_LABELS.get(k, k.value)}={v}" for k, v in nonzero)
    return (
        f"Causal session: {ledger.session_id}\n"
        f"Recorded events: {total}\n"
        f"Counts: {histogram}\n"
        f"Path:   {ledger.path}\n"
        f"Latest: {last_line}"
    )


def render_recent(ledger: CausalLedger, *, limit: int = 20) -> str:
    """Render the last *limit* events in append order."""
    events = ledger.events()
    if not events:
        return f"No events recorded in session {ledger.session_id}."
    selected = events[-max(1, limit) :]
    lines = [
        f"Last {len(selected)} of {len(events)} events (session {ledger.session_id}):",
        "",
    ]
    for e in selected:
        lines.append(_format_event_line(e))
    return "\n".join(lines)


def render_ancestry(
    ledger: CausalLedger,
    event_id: int,
) -> str:
    """Render the causal-ancestry tree rooted at ``event_id``.

    Walks parent_ids breadth-first and renders the result as an
    indented tree. Cycle-safe by virtue of the visited set.
    """
    by_id = {e.id: e for e in ledger.events()}
    root = by_id.get(event_id)
    if root is None:
        return f"No event with id {event_id} in this session."

    lines = [
        f"Why did event #{event_id} happen?",
        "",
        _format_event_line(root),
    ]
    seen: set[int] = {root.id}
    queue: deque[tuple[int, int]] = deque((p, 1) for p in root.parent_ids)
    while queue:
        cur_id, depth = queue.popleft()
        if cur_id in seen:
            continue
        seen.add(cur_id)
        cur = by_id.get(cur_id)
        if cur is None:
            lines.append("  " * depth + f"#{cur_id} (missing — event dropped from log)")
            continue
        lines.append(_format_event_line(cur, indent=depth))
        for parent in cur.parent_ids:
            if parent not in seen:
                queue.append((parent, depth + 1))
    return "\n".join(lines)


def render_graph(ledger: CausalLedger, *, limit: int = 60) -> str:
    """Render the whole session as a chronological tree.

    Each event prints on its own line; children of the same parent are
    visually grouped with prefix characters. We cap at ``limit`` events
    to keep terminal output manageable; the on-disk log is the
    durable record.
    """
    events = ledger.events()
    if not events:
        return f"No events recorded in session {ledger.session_id}."
    if limit > 0 and len(events) > limit:
        events = events[-limit:]
        header = (
            f"Causal graph (last {limit} of {len(ledger.events())} events) "
            f"— session {ledger.session_id}:"
        )
    else:
        header = f"Causal graph ({len(events)} events) — session {ledger.session_id}:"

    by_id = {e.id: e for e in events}
    children: dict[int, list[int]] = {e.id: [] for e in events}
    roots: list[int] = []
    for e in events:
        if not e.parent_ids or not any(p in by_id for p in e.parent_ids):
            roots.append(e.id)
        else:
            for p in e.parent_ids:
                if p in children:
                    children[p].append(e.id)

    lines = [header, ""]

    def _walk(node_id: int, depth: int, seen: set[int]) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = by_id.get(node_id)
        if node is None:
            return
        lines.append(_format_event_line(node, indent=depth))
        for child_id in children.get(node_id, []):
            _walk(child_id, depth + 1, seen)

    seen: set[int] = set()
    for root in roots:
        _walk(root, 0, seen)
    return "\n".join(lines)


def render_session_list(
    sessions: list[str],
    *,
    limit: int = 10,
) -> str:
    """Render the list of session ids returned by :func:`list_sessions`."""
    if not sessions:
        return (
            "No causal sessions recorded yet.\n"
            "Run '/causal on' and send a prompt to start one."
        )
    selected = sessions[:limit]
    lines = [
        f"{len(selected)} of {len(sessions)} session(s):",
        "",
    ]
    for sid in selected:
        # Best-effort age display: session ids start with an ISO-8601-ish stamp.
        try:
            tstamp_text = sid.split("-", 1)[0]
            parsed = time.strptime(tstamp_text, "%Y%m%dT%H%M%SZ")
            age = (time.time() - time.mktime(parsed)) / 60
            tag = f"~{age:.0f}m ago"
        except (IndexError, ValueError):
            tag = "?"
        lines.append(f"  {sid}   ({tag})")
    return "\n".join(lines)


__all__ = [
    "render_ancestry",
    "render_graph",
    "render_recent",
    "render_session_list",
    "render_status",
]
