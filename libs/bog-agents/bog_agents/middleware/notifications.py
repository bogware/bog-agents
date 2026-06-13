"""Middleware for developer experience enhancements.

Feature #42: Streaming token counter.
Feature #43: Session naming.
Feature #45: Progress indicators.
Feature #46: Notification system.
Feature #47: Clipboard integration.
Feature #49: Command palette.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from typing import Annotated

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    """Information about the current session."""

    name: str = ""
    session_id: str = ""
    started_at: float = field(default_factory=time.time)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0


@dataclass
class ProgressTask:
    """A tracked progress task."""

    task_id: str
    description: str
    total: int = 0
    current: int = 0
    status: str = "running"  # running, completed, failed
    started_at: float = field(default_factory=time.time)

    @property
    def percent(self) -> float:
        """Get completion percentage."""
        return (self.current / self.total * 100) if self.total > 0 else 0.0


def send_desktop_notification(title: str, message: str) -> bool:
    """Send a desktop notification.

    Args:
        title: Notification title.
        message: Notification body.

    Returns:
        True if sent successfully.
    """
    system = platform.system()
    try:
        if system == "Linux":
            subprocess.run(
                ["notify-send", title, message],
                timeout=5,
                check=False,
            )
            return True
        if system == "Darwin":
            # Pass title/message as argv to a STATIC script — never interpolate
            # into the AppleScript source, or a `"` in the text could break out
            # and run `do shell script "..."`. (REVIEW.md v2 P1-16.)
            static_script = "on run argv\n  display notification (item 1 of argv) with title (item 2 of argv)\nend run"
            subprocess.run(
                ["osascript", "-e", static_script, message, title],
                timeout=5,
                check=False,
            )
            return True
        if system == "Windows":
            # Pass values via the environment and reference them with $env: so
            # PowerShell never parses the text as code, and XML-escape them so
            # they can't break the toast template either. (REVIEW.md v2 P1-16.)
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] | Out-Null; "
                "$t = [System.Security.SecurityElement]::Escape($env:BOG_NOTIFY_TITLE); "
                "$m = [System.Security.SecurityElement]::Escape($env:BOG_NOTIFY_MESSAGE); "
                "$template = \"<toast><visual><binding template='ToastText02'>"
                "<text id='1'>$t</text><text id='2'>$m</text></binding></visual></toast>\""
            )
            subprocess.run(
                ["powershell", "-Command", ps_script],
                timeout=5,
                check=False,
                env={**os.environ, "BOG_NOTIFY_TITLE": title, "BOG_NOTIFY_MESSAGE": message},
            )
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard.

    Args:
        text: Text to copy.

    Returns:
        True if copied successfully.
    """
    try:
        import pyperclip

        pyperclip.copy(text)
        return True
    except (ImportError, Exception):
        pass

    # Fallback to system commands
    system = platform.system()
    try:
        if system == "Linux":
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE,
            )
            proc.communicate(text.encode("utf-8"))
            return proc.returncode == 0
        if system == "Darwin":
            proc = subprocess.Popen(
                ["pbcopy"],
                stdin=subprocess.PIPE,
            )
            proc.communicate(text.encode("utf-8"))
            return proc.returncode == 0
    except (FileNotFoundError, OSError):
        pass
    return False


class NotificationsState(TypedDict):
    """State for notifications middleware."""


class NotificationsMiddleware(AgentMiddleware[NotificationsState, ContextT, ResponseT]):
    """Middleware for developer experience enhancements.

    Provides session naming, progress tracking, notifications,
    and clipboard integration.
    """

    state_schema = NotificationsState

    def __init__(self, *, session_name: str = "") -> None:
        self._session = SessionInfo(name=session_name)
        self._progress_tasks: dict[str, ProgressTask] = {}
        self._progress_counter = 0
        self.tools = self._build_tools()

    @property
    def session(self) -> SessionInfo:
        """Access session info."""
        return self._session

    def _build_tools(self) -> list[BaseTool]:
        """Build DX tools."""
        middleware = self

        def session_info(
            runtime: ToolRuntime[None, NotificationsState],
        ) -> str:
            """Show current session information including token usage."""
            s = middleware._session
            elapsed = time.time() - s.started_at
            mins = int(elapsed // 60)
            return (
                f"Session: {s.name or '(unnamed)'}\n"
                f"  Duration: {mins}m {int(elapsed % 60)}s\n"
                f"  Tokens: {s.tokens_in:,} in / {s.tokens_out:,} out\n"
                f"  Cost: ${s.cost_usd:.4f}\n"
                f"  Tool calls: {s.tool_calls}"
            )

        def name_session(
            runtime: ToolRuntime[None, NotificationsState],
            name: Annotated[str, "Name for the current session"],
        ) -> str:
            """Name the current session for easy identification."""
            middleware._session.name = name
            return f"Session named: '{name}'"

        def track_progress(
            runtime: ToolRuntime[None, NotificationsState],
            description: Annotated[str, "Description of the task"],
            total: Annotated[int, "Total items to process"] = 0,
        ) -> str:
            """Start tracking progress for a long-running task."""
            middleware._progress_counter += 1
            task_id = f"task-{middleware._progress_counter}"
            middleware._progress_tasks[task_id] = ProgressTask(
                task_id=task_id,
                description=description,
                total=total,
            )
            return f"Tracking: {description} (id={task_id})"

        def update_progress(
            runtime: ToolRuntime[None, NotificationsState],
            task_id: Annotated[str, "Task ID to update"],
            current: Annotated[int, "Current progress value"],
            status: Annotated[str, "Status: 'running', 'completed', 'failed'"] = "running",
        ) -> str:
            """Update progress for a tracked task."""
            task = middleware._progress_tasks.get(task_id)
            if not task:
                return f"Task '{task_id}' not found."
            task.current = current
            task.status = status

            if task.total > 0:
                bar_len = 30
                filled = int(bar_len * task.percent / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                return f"[{bar}] {task.percent:.0f}% — {task.description}"
            return f"[{status}] {task.description}: {current} processed"

        def notify(
            runtime: ToolRuntime[None, NotificationsState],
            title: Annotated[str, "Notification title"],
            message: Annotated[str, "Notification message"],
        ) -> str:
            """Send a desktop notification."""
            sent = send_desktop_notification(title, message)
            return f"Notification {'sent' if sent else 'failed'}: {title}"

        def copy_to_clip(
            runtime: ToolRuntime[None, NotificationsState],
            text: Annotated[str, "Text to copy to clipboard"],
        ) -> str:
            """Copy text to the system clipboard."""
            copied = copy_to_clipboard(text)
            return f"{'Copied' if copied else 'Failed to copy'} {len(text)} characters to clipboard."

        return [
            StructuredTool.from_function(name="session_info", description="Show session info.", func=session_info),
            StructuredTool.from_function(name="name_session", description="Name the current session.", func=name_session),
            StructuredTool.from_function(name="track_progress", description="Start progress tracking.", func=track_progress),
            StructuredTool.from_function(name="update_progress", description="Update task progress.", func=update_progress),
            StructuredTool.from_function(name="notify", description="Send desktop notification.", func=notify),
            StructuredTool.from_function(name="copy_to_clipboard", description="Copy to clipboard.", func=copy_to_clip),
        ]
