"""Persistence for soft-rule violation logs.

When ``LawsMiddleware`` detects a Constitution violation (soft rule —
log-only, not rejected), the violation is logged via Python's logger
*and* — if a recorder is wired in — appended to an on-disk JSONL file
so the dashboard and the ``/laws violations`` slash command can show
recent entries.

Why JSONL instead of SQLite: violations are append-only, bounded in
volume (humans rarely produce > a few thousand over a project's
lifetime), and a JSONL file is dead-simple to read with ``tail``.

The file lives at::

    ~/.bog-agents/agents/<agent_id>/violations.jsonl

One line per recording event, each line a JSON object::

    {"ts": 1778670000.123, "kind": "constitution", "phrases": ["foo", "bar"]}

This module never raises into the agent's prompt path. Read/write
errors degrade to "no violations to show" rather than failing the
hosting feature.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from bog_agents_cli.dreamscape.lifecycle import agent_state_dir

logger = logging.getLogger(__name__)


_FILE_NAME = "violations.jsonl"
_MAX_FILE_BYTES = 256 * 1024  # 256 KB cap — rotate to .1 when exceeded.
_VALID_KINDS = frozenset({"constitution", "law"})


@dataclass
class ViolationEntry:
    """One recorded violation event."""

    timestamp: float
    """Unix epoch seconds when the violation was recorded."""

    kind: str
    """``"constitution"`` (soft, logged) or ``"law"`` (hard, rejected)."""

    phrases: list[str]
    """The matched rule phrases that triggered the recording."""

    @classmethod
    def from_line(cls, line: str) -> ViolationEntry | None:
        """Parse one JSONL line. Returns None on malformed input."""
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        ts = obj.get("ts")
        kind = obj.get("kind")
        phrases = obj.get("phrases")
        if not isinstance(ts, (int, float)):
            return None
        if not isinstance(kind, str) or kind not in _VALID_KINDS:
            return None
        if not isinstance(phrases, list) or not all(
            isinstance(p, str) for p in phrases
        ):
            return None
        return cls(timestamp=float(ts), kind=kind, phrases=list(phrases))


def violations_path(agent_id: str) -> Path:
    """Return ``~/.bog-agents/agents/<agent_id>/violations.jsonl``."""
    return agent_state_dir(agent_id) / _FILE_NAME


def record_violation(agent_id: str, kind: str, phrases: list[str]) -> bool:
    """Append one violation event. Returns whether the write succeeded.

    Args:
        agent_id: Per-agent identifier (same as for snapshots).
        kind: ``"constitution"`` or ``"law"``.
        phrases: The matched rule phrases.

    Returns:
        True on a successful append, False on any error or invalid input.
        Never raises.
    """
    if not phrases:
        return False
    if kind not in _VALID_KINDS:
        return False
    path = violations_path(agent_id)
    entry = {
        "ts": time.time(),
        "kind": kind,
        "phrases": list(phrases),
    }
    try:
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("violation log write failed (%s): %s", path, exc)
        return False
    return True


def load_recent_violations(
    agent_id: str, *, limit: int = 20, kind: str | None = None
) -> list[ViolationEntry]:
    """Read the most recent N violations, newest first.

    Args:
        agent_id: Per-agent identifier.
        limit: Maximum number of entries to return.
        kind: Optional filter (``"constitution"`` or ``"law"``).

    Returns:
        Newest-first list. Empty on any read error.
    """
    path = violations_path(agent_id)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("violation log read failed (%s): %s", path, exc)
        return []
    out: list[ViolationEntry] = []
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        entry = ViolationEntry.from_line(line)
        if entry is None:
            continue
        if kind is not None and entry.kind != kind:
            continue
        out.append(entry)
        if len(out) >= max(1, limit):
            break
    return out


def make_violation_recorder(agent_id: str) -> Callable[[str, list[str]], None]:
    """Return a ``(kind, phrases) -> None`` callback bound to one agent.

    Suitable for passing to ``LawsMiddleware(violation_recorder=...)``.
    Swallows failures — never raises into the prompt path.
    """

    def _recorder(kind: str, phrases: list[str]) -> None:
        with suppress(Exception):
            record_violation(agent_id, kind, phrases)

    return _recorder


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
