"""Stand-alone controller for the ``/goal`` and ``/rubric`` commands.

The CLI keeps a durable **goal** — an objective plus an acceptance-criteria
rubric — in a per-project JSON file (``.bog-agents/goal.json``) so it survives
restarts and is readable by the headless ``goal`` twin. The SDK's
:class:`~bog_agents.middleware.goal_tools.GoalToolsMiddleware` reads the same
objective/rubric out of checkpointed graph state; ``app.py`` seeds those
channels from this file (see :func:`state_seed`) so the agent's
``get_goal``/``get_rubric`` tools stay in sync with what the user set.

Everything here is pure and file-backed so it is unit-testable without spinning
up the TUI — the pattern CLAUDE.md recommends (see ``expert_controller.py``).
The thin ``app.py`` handlers load a :class:`GoalRecord`, mutate it through the
free functions below, and render the result.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)

GOAL_STATUSES: tuple[str, ...] = ("active", "blocked", "complete")
"""Recognized goal lifecycle statuses (mirrors the SDK's ``GoalStatus``)."""

_GOAL_FILE = Path(".bog-agents") / "goal.json"


@dataclass
class GoalRecord:
    """A durable goal: an objective, its rubric, and the latest status/note.

    Attributes:
        objective: The goal objective. Empty string when no goal is set.
        rubric: Acceptance criteria, one criterion per entry.
        status: Lifecycle status — one of :data:`GOAL_STATUSES`.
        note: Latest progress or blocker note (usually agent-recorded).
        updated_at: Unix timestamp of the last mutation.
    """

    objective: str = ""
    rubric: list[str] = field(default_factory=list)
    status: str = "active"
    note: str = ""
    updated_at: float = 0.0

    @property
    def is_set(self) -> bool:
        """Whether an objective has been set."""
        return bool(self.objective.strip())


# ---------------------------------------------------------------------------
# Parsing / normalization helpers
# ---------------------------------------------------------------------------


def parse_rubric_lines(text: str) -> list[str]:
    """Split free-form criteria text into a clean list of criterion strings.

    Accepts newline- or semicolon-separated input and strips common bullet
    markers (``-``, ``*``, ``•``) and leading list numbering so pasted
    markdown lists round-trip cleanly.

    Args:
        text: Raw criteria text.

    Returns:
        Non-empty criterion strings in order.
    """
    raw = text.replace(";", "\n")
    criteria: list[str] = []
    for line in raw.splitlines():
        cleaned = line.strip().lstrip("-*•").strip()
        # Drop a leading "1." / "2)" ordinal if present.
        head, sep, rest = cleaned.partition(".")
        if sep and head.strip().isdigit():
            cleaned = rest.strip()
        else:
            head, sep, rest = cleaned.partition(")")
            if sep and head.strip().isdigit():
                cleaned = rest.strip()
        if cleaned:
            criteria.append(cleaned)
    return criteria


def _coerce_status(value: object) -> str:
    """Normalize a persisted status value to a known status (default active)."""
    if isinstance(value, str) and value.strip() in GOAL_STATUSES:
        return value.strip()
    return "active"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def goal_path(working_dir: Path | str) -> Path:
    """Return the goal file path for ``working_dir``."""
    return Path(working_dir) / _GOAL_FILE


def load_goal(working_dir: Path | str) -> GoalRecord:
    """Load the goal for ``working_dir`` (an empty record when none is set).

    Args:
        working_dir: Project root.

    Returns:
        The persisted :class:`GoalRecord`, or a fresh empty one when the file
        is missing or unreadable.
    """
    path = goal_path(working_dir)
    if not path.exists():
        return GoalRecord()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read goal file %s", path, exc_info=True)
        return GoalRecord()
    if not isinstance(data, dict):
        return GoalRecord()
    rubric_raw = data.get("rubric")
    rubric = (
        [str(c).strip() for c in rubric_raw if isinstance(c, str) and str(c).strip()]
        if isinstance(rubric_raw, list)
        else []
    )
    return GoalRecord(
        objective=str(data.get("objective", "")).strip(),
        rubric=rubric,
        status=_coerce_status(data.get("status")),
        note=str(data.get("note", "")).strip(),
        updated_at=float(data.get("updated_at", 0.0) or 0.0),
    )


def save_goal(working_dir: Path | str, record: GoalRecord) -> None:
    """Persist ``record`` for ``working_dir`` atomically.

    Args:
        working_dir: Project root.
        record: The goal to persist. ``updated_at`` is stamped on write.
    """
    record.updated_at = time.time()
    payload = {
        "objective": record.objective,
        "rubric": record.rubric,
        "status": record.status,
        "note": record.note,
        "updated_at": record.updated_at,
    }
    atomic_write_text(
        goal_path(working_dir),
        json.dumps(payload, indent=2, ensure_ascii=False),
    )


def clear_goal(working_dir: Path | str) -> None:
    """Delete the persisted goal for ``working_dir`` (a no-op when absent)."""
    path = goal_path(working_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete goal file %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# Mutations (each returns the updated record and persists it)
# ---------------------------------------------------------------------------


def set_objective(working_dir: Path | str, objective: str) -> GoalRecord:
    """Set the goal objective, preserving any existing rubric.

    Args:
        working_dir: Project root.
        objective: New objective text.

    Returns:
        The updated record.
    """
    record = load_goal(working_dir)
    record.objective = objective.strip()
    # A freshly (re)set objective is active again unless it was already set.
    if not record.status or record.status == "complete":
        record.status = "active"
    save_goal(working_dir, record)
    return record


def set_rubric(working_dir: Path | str, criteria: list[str]) -> GoalRecord:
    """Replace the acceptance criteria for the current goal.

    Args:
        working_dir: Project root.
        criteria: New criteria (each entry a criterion string).

    Returns:
        The updated record.
    """
    record = load_goal(working_dir)
    record.rubric = [c.strip() for c in criteria if c.strip()]
    save_goal(working_dir, record)
    return record


def set_status(working_dir: Path | str, status: str, note: str = "") -> GoalRecord:
    """Update the goal status (and optionally the note).

    Args:
        working_dir: Project root.
        status: New status; unknown values normalize to ``active``.
        note: Optional progress/blocker note.

    Returns:
        The updated record.
    """
    record = load_goal(working_dir)
    record.status = _coerce_status(status)
    if note.strip():
        record.note = note.strip()
    save_goal(working_dir, record)
    return record


def merge_agent_state(
    record: GoalRecord, state_values: dict[str, Any] | None
) -> GoalRecord:
    """Fold agent-recorded status/note from live graph state into ``record``.

    The objective and rubric are user-controlled (this file is the source of
    truth), but ``status`` and ``note`` may have been advanced by the agent's
    ``update_goal`` tool during a turn. When live state carries a more recent
    status/note, reflect it in the display without persisting.

    Args:
        record: The file-backed record to augment (not mutated).
        state_values: Checkpointed graph state values, or ``None``.

    Returns:
        A copy of ``record`` with agent status/note folded in when present.
    """
    if not isinstance(state_values, dict):
        return record
    merged = GoalRecord(
        objective=record.objective,
        rubric=list(record.rubric),
        status=record.status,
        note=record.note,
        updated_at=record.updated_at,
    )
    agent_status = state_values.get("_goal_status")
    if isinstance(agent_status, str) and agent_status.strip() in GOAL_STATUSES:
        merged.status = agent_status.strip()
    agent_note = state_values.get("_goal_note")
    if isinstance(agent_note, str) and agent_note.strip():
        merged.note = agent_note.strip()
    return merged


def state_seed(record: GoalRecord) -> dict[str, Any]:
    """Build the checkpointed-state update that mirrors ``record`` to the agent.

    The keys match :class:`~bog_agents.middleware.goal_tools.GoalState`'s
    private channels so ``GoalToolsMiddleware``'s ``get_goal``/``get_rubric``
    tools and the per-turn system-prompt injection see the user's goal.

    Args:
        record: The goal to mirror.

    Returns:
        A state-update dict suitable for ``agent.aupdate_state``.
    """
    return {
        "_goal_objective": record.objective or None,
        "_goal_rubric": list(record.rubric) or None,
        "_goal_status": record.status if record.is_set else None,
        "_goal_note": record.note or None,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_goal(record: GoalRecord) -> str:
    """Render the current goal for the ``/goal`` display."""
    if not record.is_set:
        return (
            "No goal set. Use [bold]/goal <objective>[/bold] to set one, then "
            "[bold]/rubric draft[/bold] to draft acceptance criteria."
        )
    lines = [
        "[bold]Goal[/bold]",
        f"> {record.objective}",
        f"Status: {record.status}",
    ]
    if record.rubric:
        lines.append("Acceptance criteria:")
        lines.extend(f"  {i}. {c}" for i, c in enumerate(record.rubric, start=1))
    else:
        lines.append("Acceptance criteria: (none — /rubric draft)")
    if record.note:
        lines.append(f"Latest note: {record.note}")
    return "\n".join(lines)


def render_rubric(record: GoalRecord) -> str:
    """Render the current rubric for the ``/rubric`` display."""
    if not record.rubric:
        if not record.is_set:
            return "No goal set — set one with /goal <objective> first."
        return (
            "No acceptance criteria yet. Draft them from the goal with "
            "[bold]/rubric draft[/bold], or set them with "
            "[bold]/rubric set <criteria>[/bold]."
        )
    lines = ["[bold]Acceptance criteria[/bold]"]
    lines.extend(f"  {i}. {c}" for i, c in enumerate(record.rubric, start=1))
    return "\n".join(lines)


__all__ = [
    "GOAL_STATUSES",
    "GoalRecord",
    "clear_goal",
    "goal_path",
    "load_goal",
    "merge_agent_state",
    "parse_rubric_lines",
    "render_goal",
    "render_rubric",
    "save_goal",
    "set_objective",
    "set_rubric",
    "set_status",
    "state_seed",
]
