"""Git-backed undo for the last file edit made by the agent."""

from __future__ import annotations

import subprocess  # noqa: S404
from pathlib import Path

# Module-level state tracking the most recent recorded edit.
# Keys: "path" (str), "original" (str | None)
_last_edit: dict | None = None


def record_edit(file_path: str) -> None:
    """Record a file path before the agent edits it.

    Reads the current content and stores it so `undo_last_edit` can restore
    the file to its pre-edit state.  If the file does not yet exist the
    original is stored as ``None``; undoing will then delete the new file.

    Args:
        file_path: Absolute or relative path to the file about to be edited.
    """
    global _last_edit  # noqa: PLW0603

    p = Path(file_path)
    if p.exists():
        try:
            original: str | None = p.read_text(encoding="utf-8")
        except OSError:
            original = None
    else:
        original = None

    _last_edit = {"path": file_path, "original": original}


def undo_last_edit() -> str:
    """Undo the last recorded file edit.

    Restores the file to the content captured by the most recent call to
    `record_edit`.  If the file was newly created (original was ``None``) it
    is deleted instead.

    Returns:
        Rich-formatted result string describing what was done (or why nothing
        was done).
    """
    global _last_edit  # noqa: PLW0603

    if _last_edit is None:
        return "Nothing to undo."

    file_path = _last_edit["path"]
    original = _last_edit["original"]
    _last_edit = None

    p = Path(file_path)

    if original is None:
        # File was newly created — delete it.
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            return f"[red]Undo failed:[/red] could not delete [cyan]{file_path}[/cyan]: {exc}"
        return f"[green]Undo:[/green] deleted new file [cyan]{file_path}[/cyan]."

    # File was modified — restore previous content.
    try:
        from bog_agents_cli.io_utils import atomic_write_text

        atomic_write_text(p, original)
    except OSError as exc:
        return (
            f"[red]Undo failed:[/red] could not restore [cyan]{file_path}[/cyan]: {exc}"
        )

    return f"[green]Undo:[/green] restored [cyan]{file_path}[/cyan] to its previous content."


def get_last_edit_summary() -> str | None:
    """Return a one-line summary of what would be undone, or None.

    Returns:
        A human-readable summary string, or ``None`` if there is nothing to
        undo.
    """
    if _last_edit is None:
        return None
    file_path = _last_edit["path"]
    original = _last_edit["original"]
    if original is None:
        return f"Delete newly created file: {file_path}"
    return f"Restore previous content of: {file_path}"


def undo_via_git(file_path: str) -> str:
    """Undo changes to a specific file using git checkout.

    Runs ``git checkout -- <file_path>`` to discard working-tree modifications
    and revert the file to the last committed version.

    Args:
        file_path: Path to the file to revert (passed directly to git).

    Returns:
        Rich-formatted result string.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "checkout", "--", file_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return "[red]Undo via git failed:[/red] `git` executable not found."
    except OSError as exc:
        return f"[red]Undo via git failed:[/red] {exc}"

    if result.returncode == 0:
        return f"[green]Git undo:[/green] reverted [cyan]{file_path}[/cyan] to last committed version."

    stderr = result.stderr.strip()
    return f"[red]Git undo failed[/red] for [cyan]{file_path}[/cyan]: {stderr or 'unknown error'}"
