"""Turn-end changes tray (ROADMAP #66).

Every turn that wrote files ends with a reviewable changeset: per-file stats
in explanatory order (entry points and public signatures first; tests,
snapshots and lockfiles last), a coloured diff per file on demand, and
per-file or per-hunk revert straight from the tray. The data comes from the
adapter's `FileOpTracker` records (before/after content per operation), so
it works for untracked files and needs no git.

`/changes` verbs: `` (tray) · `show <n>` · `revert <n>` · `revert <n> <hunk>` ·
`keep` · `/diff --ordered` shares the same ranking.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bog_agents.diff_ordering import (
    FileChange,
    is_muted,
    rank_changes,
    reorder_unified_diff,
)

from bog_agents_cli.diff_hunks import parse_hunks, revert_hunk
from bog_agents_cli.file_ops import compute_unified_diff

logger = logging.getLogger(__name__)


@dataclass
class ChangedFile:
    """One file touched during a turn, collapsed to its net change."""

    display_path: str
    physical_path: Path | None
    before: str
    after: str
    diff: str = ""
    added: int = 0
    removed: int = 0

    @property
    def muted(self) -> bool:
        """Lockfile / snapshot / generated output (shown last, collapsed)."""
        return is_muted(self.display_path)

    @property
    def hunk_count(self) -> int:
        """Number of hunks in the diff."""
        return len(parse_hunks(self.diff))


@dataclass
class TurnChanges:
    """The tray's model: ranked files plus a timestamp."""

    files: list[ChangedFile] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def added(self) -> int:
        """Total added lines."""
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        """Total removed lines."""
        return sum(f.removed for f in self.files)


def _counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def collect_turn_changes(records: list[Any]) -> TurnChanges:
    """Fold a turn's `FileOperationRecord`s into one net change per file.

    Args:
        records: Completed tracker records (reads are ignored; the first
            `before_content` and the last `after_content` per path win).

    Returns:
        The ranked `TurnChanges` (empty when nothing changed).
    """
    firsts: dict[str, ChangedFile] = {}
    for rec in records:
        if getattr(rec, "tool_name", "") not in {"write_file", "edit_file"}:
            continue
        if getattr(rec, "status", "") == "error":
            continue
        before = getattr(rec, "before_content", None)
        after = getattr(rec, "after_content", None)
        if after is None:
            continue
        key = str(
            getattr(rec, "physical_path", None) or getattr(rec, "display_path", "")
        )
        entry = firsts.get(key)
        if entry is None:
            firsts[key] = ChangedFile(
                display_path=str(getattr(rec, "display_path", key)),
                physical_path=getattr(rec, "physical_path", None),
                before=before or "",
                after=after,
            )
        else:
            entry.after = after
    files: list[ChangedFile] = []
    for entry in firsts.values():
        if entry.before == entry.after:
            continue
        entry.diff = (
            compute_unified_diff(
                entry.before, entry.after, entry.display_path, max_lines=None
            )
            or ""
        )
        entry.added, entry.removed = _counts(entry.diff)
        files.append(entry)
    ranked = rank_changes(
        [FileChange(f.display_path, f.added, f.removed, f.diff) for f in files]
    )
    order = {c.path: i for i, c in enumerate(ranked)}
    files.sort(key=lambda f: order.get(f.display_path, len(order)))
    return TurnChanges(files=files)


def render_tray(changes: TurnChanges) -> str:
    """Render the tray as Rich markup."""
    from rich.markup import escape

    if not changes.files:
        return "No file changes this turn."
    lines = [
        f"[bold]Changes this turn[/bold]  {len(changes.files)} file(s)  [green]+{changes.added}[/green]/[red]-{changes.removed}[/red]"
    ]
    for i, f in enumerate(changes.files, start=1):
        tag = " [dim](muted)[/dim]" if f.muted else ""
        hunks = f.hunk_count
        hunk_text = f"{hunks} hunk{'s' if hunks != 1 else ''}"
        lines.append(
            f"  {i:>2}. {escape(f.display_path)}  [green]+{f.added}[/green]/[red]-{f.removed}[/red]  [dim]{hunk_text}[/dim]{tag}"
        )
    lines.append(
        "[dim]/changes show <n> · /changes revert <n> [hunk] · /changes keep · /diff --ordered[/dim]"
    )
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _target_path(app: Any, entry: ChangedFile) -> Path:  # noqa: ANN401 - the App
    if entry.physical_path is not None:
        return Path(entry.physical_path)
    return Path(getattr(app, "_cwd", ".")) / entry.display_path


def revert_file(app: Any, entry: ChangedFile) -> str:  # noqa: ANN401 - the App
    """Restore the file's pre-turn content."""
    target = _target_path(app, entry)
    try:
        _write(target, entry.before)
    except OSError as exc:
        return f"Could not restore {entry.display_path}: {exc}"
    entry.after = entry.before
    entry.diff, entry.added, entry.removed = "", 0, 0
    return f"Restored {entry.display_path} to its pre-turn content."


def revert_one_hunk(app: Any, entry: ChangedFile, hunk_number: int) -> str:  # noqa: ANN401 - the App
    """Undo hunk `hunk_number` (1-based) of the file's diff on disk."""
    hunks = parse_hunks(entry.diff)
    if not 1 <= hunk_number <= len(hunks):
        return f"{entry.display_path} has {len(hunks)} hunk(s); pick 1..{len(hunks)}."
    target = _target_path(app, entry)
    try:
        current = target.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read {entry.display_path}: {exc}"
    reverted = revert_hunk(current, hunks[hunk_number - 1])
    if reverted is None:
        return f"Hunk {hunk_number} of {entry.display_path} no longer matches the file; revert the whole file or edit by hand."
    try:
        _write(target, reverted)
    except OSError as exc:
        return f"Could not write {entry.display_path}: {exc}"
    entry.after = reverted
    entry.diff = (
        compute_unified_diff(
            entry.before, entry.after, entry.display_path, max_lines=None
        )
        or ""
    )
    entry.added, entry.removed = _counts(entry.diff)
    return f"Reverted hunk {hunk_number} of {entry.display_path}."


def handle_changes_command(
    app: Any,  # noqa: ANN401 - the App
    command: str,
) -> tuple[str | None, tuple[str, str] | None]:
    """Dispatch `/changes` verbs.

    Args:
        app: The app (holds `_last_changes`).
        command: The raw slash command.

    Returns:
        `(text, diff)`: text to mount (or `None`) and an optional
        `(diff_text, display_path)` to render as a coloured diff.
    """
    changes: TurnChanges | None = getattr(app, "_last_changes", None)
    parts = command.strip().split()
    verb = parts[1].lower() if len(parts) > 1 else ""
    if verb in {"", "list", "show"} and len(parts) <= 2 and verb != "show":
        if changes is None:
            return (
                "No changes recorded yet — the tray appears after a turn that edits files.",
                None,
            )
        return render_tray(changes), None
    if changes is None or not changes.files:
        return "No changes recorded yet.", None
    if verb == "keep":
        app._last_changes = None
        return "Kept all changes; tray cleared.", None
    if verb in {"show", "revert"}:
        try:
            index = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            index = 0
        if not 1 <= index <= len(changes.files):
            return f"Usage: /changes {verb} <n>  (1..{len(changes.files)})", None
        entry = changes.files[index - 1]
        if verb == "show":
            return None, (entry.diff or "(no diff)", entry.display_path)
        if len(parts) > 3:
            try:
                hunk_number = int(parts[3])
            except ValueError:
                return "Usage: /changes revert <n> [hunk]", None
            return revert_one_hunk(app, entry, hunk_number), None
        return revert_file(app, entry), None
    return (
        "Usage: /changes | /changes show <n> | /changes revert <n> [hunk] | /changes keep",
        None,
    )


async def run_changes_command(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Mount the result of a `/changes` verb."""
    from bog_agents_cli.widgets.messages import AppMessage, DiffMessage

    text, diff = handle_changes_command(app, command)
    if diff is not None:
        await app._mount_message(DiffMessage(diff[0], diff[1], max_lines=600))
    if text:
        await app._mount_message(AppMessage(text))


async def mount_changes_tray(app: Any, turn_stats: Any) -> None:  # noqa: ANN401 - the App / SessionStats
    """Turn-end hook: build the tray from the turn's file records and show it."""
    from bog_agents_cli.widgets.messages import AppMessage

    records = list(getattr(turn_stats, "file_records", None) or [])
    if not records:
        return
    try:
        changes = collect_turn_changes(records)
    except Exception:
        logger.debug("changes tray: collect failed", exc_info=True)
        return
    if not changes.files:
        return
    app._last_changes = changes
    await app._mount_message(AppMessage(render_tray(changes)))


def maybe_reorder_diff(raw_arg: str, output: str) -> str:
    """`/diff --ordered`: reorder a git diff's file blocks by explanatory power."""
    if raw_arg.strip().lower() in {"ordered", "--ordered"}:
        return reorder_unified_diff(output)
    return output
