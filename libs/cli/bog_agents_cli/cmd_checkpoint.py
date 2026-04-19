"""Session checkpoint system — name, save, and restore conversation threads."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

_CHECKPOINTS_PATH: Path = Path.home() / ".bog-agents" / "checkpoints.json"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _load() -> dict[str, dict]:
    """Load the checkpoints index from disk.

    Returns:
        Dict mapping checkpoint name to its metadata dict.
    """
    if not _CHECKPOINTS_PATH.exists():
        return {}
    try:
        return json.loads(_CHECKPOINTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, dict]) -> None:
    """Persist the checkpoints index to disk atomically.

    Args:
        data: Full checkpoints dict to write.
    """
    from bog_agents_cli.io_utils import atomic_write_text

    atomic_write_text(_CHECKPOINTS_PATH, json.dumps(data, indent=2, ensure_ascii=False))


def list_checkpoints() -> str:
    """Return a Rich-formatted table of saved checkpoints.

    Returns:
        Rich markup string with checkpoint rows, or a hint message when empty.
    """
    data = _load()
    if not data:
        return "No checkpoints saved yet. Use /checkpoint save <name> to create one."

    header = f"  {'Name':<24}  {'Thread ID':<14}  {'Created':<20}  Description"
    sep = "  " + "\u2500" * 80
    lines: list[str] = [
        "[bold]Checkpoints[/bold]",
        "",
        header,
        sep,
    ]
    for name, meta in sorted(data.items()):
        thread_id = meta.get("thread_id", "")
        short_tid = thread_id[:12] + "\u2026" if len(thread_id) > 12 else thread_id
        created_at = meta.get("created_at", "")
        description = meta.get("description", "")
        lines.append(
            f"  [cyan]{name:<24}[/cyan]  {short_tid:<14}  {created_at:<20}  [dim]{description}[/dim]"
        )

    return "\n".join(lines)


def save_checkpoint(thread_id: str, name: str, *, description: str = "") -> str:
    """Save the current thread as a named checkpoint.

    Saves to `~/.bog-agents/checkpoints.json`. Overwrites any existing entry
    with the same name.

    Args:
        thread_id: The active LangGraph thread UUID string.
        name: Human-readable checkpoint name (alphanumeric, hyphens, underscores; 1-64 chars).
        description: Optional free-text description stored alongside the checkpoint.

    Returns:
        Rich markup success or error message.
    """
    if not _NAME_RE.match(name):
        return (
            "[red]Invalid checkpoint name.[/red] "
            "Use only letters, digits, hyphens, and underscores (1-64 chars)."
        )

    data = _load()
    overwrite = name in data
    data[name] = {
        "thread_id": thread_id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "description": description,
    }
    try:
        _save(data)
    except OSError as exc:
        return f"[red]Failed to save checkpoint:[/red] {exc}"

    if overwrite:
        return f"[green]Checkpoint '[cyan]{name}[/cyan]' updated.[/green] (overwrote existing entry)"
    return f"[green]Checkpoint '[cyan]{name}[/cyan]' saved.[/green] Thread: {thread_id}"


def load_checkpoint(name: str) -> str | None:
    """Return the thread_id for a named checkpoint, or None if not found.

    Args:
        name: Checkpoint name to look up.

    Returns:
        The stored thread_id string, or None when the name is not found.
    """
    data = _load()
    entry = data.get(name)
    if entry is None:
        return None
    return entry.get("thread_id")


def delete_checkpoint(name: str) -> str:
    """Delete a named checkpoint.

    Args:
        name: Checkpoint name to remove.

    Returns:
        Rich markup success or error message.
    """
    data = _load()
    if name not in data:
        return f"[red]Checkpoint '[cyan]{name}[/cyan]' not found.[/red]"
    del data[name]
    try:
        _save(data)
    except OSError as exc:
        return f"[red]Failed to delete checkpoint:[/red] {exc}"
    return f"[green]Checkpoint '[cyan]{name}[/cyan]' deleted.[/green]"


def format_checkpoint_help() -> str:
    """Return a usage help string for checkpoint commands.

    Returns:
        Plain Rich markup usage text.
    """
    return """\
[bold]Checkpoint commands[/bold]

  [cyan]/checkpoint list[/cyan]                       List all saved checkpoints
  [cyan]/checkpoint save <name> [desc][/cyan]         Save the current thread as a named checkpoint
  [cyan]/checkpoint load <name>[/cyan]                Restore a checkpoint (switch to its thread)
  [cyan]/checkpoint delete <name>[/cyan]              Delete a saved checkpoint

[bold]Name rules[/bold]  Letters, digits, hyphens, underscores — 1 to 64 characters.

[bold]Storage[/bold]  Checkpoints are stored in [dim]~/.bog-agents/checkpoints.json[/dim]."""
