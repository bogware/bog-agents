"""Parallel agents progress panel widget for bog-agents-cli.

Displays a live-updating table of parallel worktree/background agent task
statuses inside the Textual TUI, similar to Claude Code's agents sidebar.
"""

from __future__ import annotations

import time
from typing import Any

from rich.table import Table
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

_RESULT_MAX_LEN = 60
_STATUS_STYLES: dict[str, str] = {
    "running": "yellow",
    "queued": "dim",
    "pending": "dim",
    "completed": "green",
    "failed": "red",
    "cancelled": "dim",
    "unknown": "dim",
}


def _fmt_duration(started_at: float | None, finished_at: float | None) -> str:
    """Format a human-readable duration string.

    Args:
        started_at: Unix timestamp when the task started, or ``None``.
        finished_at: Unix timestamp when the task finished, or ``None``.

    Returns:
        Duration string like ``"12s"`` or ``"-"`` if task not started.
    """
    if started_at is None:
        return "-"
    end = finished_at if finished_at is not None else time.time()
    secs = max(0.0, end - started_at)
    if secs < 60:
        return f"{secs:.0f}s"
    mins = int(secs // 60)
    rem = int(secs % 60)
    return f"{mins}m{rem:02d}s"


def _fmt_result(task: dict[str, Any]) -> str:
    """Format the result/error column value for a task.

    Args:
        task: Task dict with ``"status"``, ``"result"``, and ``"error"`` keys.

    Returns:
        Truncated result string or error prefix.
    """
    status = str(task.get("status", "")).lower()
    if status == "failed":
        err = str(task.get("error") or "")
        text = f"ERR: {err}" if err else "failed"
    else:
        text = str(task.get("result") or "")
    if len(text) > _RESULT_MAX_LEN:
        text = text[:_RESULT_MAX_LEN] + "..."
    return text


def format_agents_status_table(tasks: list[dict[str, Any]]) -> str:
    """Build a Rich markup string showing all agent task statuses.

    This is a pure function suitable for use in both the ``AgentsPanel``
    widget and plain text ``/agent status`` responses.

    Args:
        tasks: List of task dicts, each with keys:
            ``task_id``, ``label``, ``status``, ``branch``,
            ``started_at``, ``finished_at``, ``result``, ``error``.

    Returns:
        Rich console markup string containing the rendered table.
    """
    table = Table(
        show_header=True,
        header_style="bold",
        show_lines=False,
        box=None,
        expand=False,
    )
    table.add_column("ID", style="cyan", no_wrap=True, min_width=6)
    table.add_column("Label", no_wrap=True, min_width=12)
    table.add_column("Status", no_wrap=True, min_width=9)
    table.add_column("Branch", no_wrap=True, min_width=12)
    table.add_column("Duration", no_wrap=True, min_width=8)
    table.add_column("Result", no_wrap=False, min_width=20)

    for task in tasks:
        task_id = str(task.get("task_id") or "-")
        label = str(task.get("label") or "-")
        status_raw = str(task.get("status") or "unknown").lower()
        branch = str(task.get("branch") or "-")
        started_at = task.get("started_at")
        finished_at = task.get("finished_at")
        duration = _fmt_duration(started_at, finished_at)
        result_text = _fmt_result(task)

        style = _STATUS_STYLES.get(status_raw, "dim")
        status_cell = Text(status_raw, style=style)

        table.add_row(task_id, label, status_cell, branch, duration, result_text)

    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True, width=120)
    console.print(table)
    return buf.getvalue()


class AgentsPanel(Static):
    """Live-updating panel showing parallel agent task statuses.

    Renders a Rich table inside a Textual ``Static`` widget. Call
    ``refresh_tasks()`` to update the display with new task data.

    Example:
        ```python
        panel = AgentsPanel()
        await app.mount(panel)
        panel.refresh_tasks(tasks)
        ```
    """

    DEFAULT_CSS = """
    AgentsPanel {
        height: auto;
        min-height: 5;
        border: solid $accent;
        padding: 0 1;
    }
    """

    _tasks: reactive[list[dict[str, Any]]] = reactive([], layout=True)

    def __init__(
        self,
        tasks: list[dict[str, Any]] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,  # noqa: A002
        classes: str | None = None,
    ) -> None:
        """Initialise the panel.

        Args:
            tasks: Initial list of task dicts to display.
            name: Widget name.
            id: CSS ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._tasks = list(tasks or [])

    def refresh_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Update the displayed task list and re-render.

        Args:
            tasks: Updated list of task dicts.
        """
        self._tasks = list(tasks)
        if self._tasks:
            self.update(format_agents_status_table(self._tasks))
        else:
            self.update("No parallel agent tasks.")

    def on_mount(self) -> None:
        """Render initial content when the widget is mounted."""
        if self._tasks:
            self.update(format_agents_status_table(self._tasks))
        else:
            self.update("No parallel agent tasks.")
