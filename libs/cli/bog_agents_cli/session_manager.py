"""Session management with naming and progress tracking.

Feature #42: Streaming token counter.
Feature #43: Session naming.
Feature #44: Rich markdown rendering.
Feature #45: Progress indicators.
Feature #46: Notification system.
Feature #47: Clipboard integration.
Feature #49: Command palette.
"""

from __future__ import annotations

import logging
import platform
import subprocess  # noqa: S404
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SessionStats:
    """Session statistics."""

    name: str = ""
    started_at: float = field(default_factory=time.time)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    messages: int = 0

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed time in seconds."""
        return time.time() - self.started_at

    @property
    def elapsed_display(self) -> str:
        """Get formatted elapsed time."""
        secs = self.elapsed_seconds
        mins = int(secs // 60)
        remaining_secs = int(secs % 60)
        if mins > 60:
            hours = mins // 60
            mins = mins % 60
            return f"{hours}h {mins}m"
        return f"{mins}m {remaining_secs}s"


@dataclass
class CommandPaletteEntry:
    """An entry in the command palette."""

    name: str
    description: str
    shortcut: str = ""
    category: str = "general"


# Built-in command palette entries
COMMAND_PALETTE: list[CommandPaletteEntry] = [
    CommandPaletteEntry("/help", "Show help information", "?", "general"),
    CommandPaletteEntry("/model", "Switch model", "", "config"),
    CommandPaletteEntry("/context", "Show context usage", "", "info"),
    CommandPaletteEntry("/health", "Codebase health report", "", "analysis"),
    CommandPaletteEntry("/test", "Run tests with coverage", "", "quality"),
    CommandPaletteEntry("/pr", "Pull request management", "", "git"),
    CommandPaletteEntry("/agent", "Agent thread management", "", "agent"),
    CommandPaletteEntry("/plugin", "Plugin management", "", "config"),
    CommandPaletteEntry("/team", "Team settings", "", "enterprise"),
    CommandPaletteEntry("/migrate", "Migration assistant", "", "analysis"),
    CommandPaletteEntry("/onboard", "Codebase onboarding", "", "info"),
    CommandPaletteEntry("/changelog", "Generate changelog", "", "git"),
    CommandPaletteEntry("/image", "Image analysis", "", "multimodal"),
    CommandPaletteEntry("/api", "API testing", "", "web"),
    CommandPaletteEntry("/preview", "Dev server preview", "", "web"),
    CommandPaletteEntry("/session", "Session info", "", "info"),
    CommandPaletteEntry("/cost", "Cost breakdown", "", "info"),
    CommandPaletteEntry("/compact", "Compact conversation", "", "config"),
    CommandPaletteEntry("/diff", "Show diff preview", "", "git"),
    CommandPaletteEntry("/profile", "Switch profile", "", "config"),
    CommandPaletteEntry("/review", "Code review", "", "quality"),
    CommandPaletteEntry("/doctor", "Diagnose setup", "", "config"),
    CommandPaletteEntry("/effort", "Set effort level", "", "config"),
]


def format_session_stats(stats: SessionStats) -> str:
    """Format session stats for display.

    Args:
        stats: Session statistics.

    Returns:
        Formatted string.
    """
    return (
        f"Session: {stats.name or '(unnamed)'}\n"
        f"  Duration: {stats.elapsed_display}\n"
        f"  Messages: {stats.messages}\n"
        f"  Tokens: {stats.tokens_in:,} in / {stats.tokens_out:,} out\n"
        f"  Cost: ${stats.cost_usd:.4f}\n"
        f"  Tool calls: {stats.tool_calls}"
    )


def format_token_counter(tokens_in: int, tokens_out: int, cost_usd: float) -> str:
    """Format a streaming token counter display.

    Args:
        tokens_in: Input tokens.
        tokens_out: Output tokens.
        cost_usd: Accumulated cost.

    Returns:
        Formatted counter string.
    """
    return f"[{tokens_in:,}→ {tokens_out:,}← ${cost_usd:.4f}]"


def send_notification(title: str, message: str) -> bool:
    """Send a desktop notification.

    Args:
        title: Notification title.
        message: Notification body.

    Returns:
        True if sent.
    """
    system = platform.system()
    try:
        if system == "Linux":
            subprocess.run(  # noqa: S603
                ["notify-send", title, message],
                timeout=5,
                check=False,
            )
            return True
        if system == "Darwin":
            script = f'display notification "{message}" with title "{title}"'
            subprocess.run(  # noqa: S603
                ["osascript", "-e", script],
                timeout=5,
                check=False,
            )
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def search_command_palette(query: str) -> list[CommandPaletteEntry]:
    """Search the command palette with fuzzy matching.

    Args:
        query: Search query.

    Returns:
        Matching commands.
    """
    query_lower = query.lower()
    results: list[tuple[int, CommandPaletteEntry]] = []

    for entry in COMMAND_PALETTE:
        score = 0
        if query_lower in entry.name.lower():
            score += 10
        if query_lower in entry.description.lower():
            score += 5
        if query_lower in entry.category.lower():
            score += 3
        if score > 0:
            results.append((score, entry))

    results.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in results]


def format_command_palette(entries: list[CommandPaletteEntry]) -> str:
    """Format command palette results for display.

    Args:
        entries: Matching entries.

    Returns:
        Formatted string.
    """
    if not entries:
        return "No matching commands."
    lines = ["Command Palette:"]
    for entry in entries:
        shortcut = f" ({entry.shortcut})" if entry.shortcut else ""
        lines.append(
            f"  {entry.name}{shortcut} — {entry.description} [{entry.category}]"
        )
    return "\n".join(lines)
