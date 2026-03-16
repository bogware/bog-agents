"""Smart context and RAG CLI interface.

Feature #13: Large context window support.
Feature #15: Smart context retrieval.
Feature #17: Context window visualization.
Feature #18: Auto-memory extraction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ContextInfo:
    """Context window usage information."""

    max_tokens: int = 200000
    used_tokens: int = 0
    sources: dict[str, int] = field(default_factory=dict)

    @property
    def percent_used(self) -> float:
        """Get percentage used."""
        return (
            (self.used_tokens / self.max_tokens * 100) if self.max_tokens > 0 else 0.0
        )

    @property
    def remaining(self) -> int:
        """Get remaining tokens."""
        return max(0, self.max_tokens - self.used_tokens)


def format_context_bar(info: ContextInfo, width: int = 40) -> str:
    """Format a context usage bar.

    Args:
        info: Context usage info.
        width: Bar width in characters.

    Returns:
        Formatted context bar string.
    """
    filled = int(width * info.percent_used / 100)
    bar = "█" * filled + "░" * (width - filled)
    return (
        f"Context: [{bar}] {info.percent_used:.1f}%\n"
        f"  {info.used_tokens:,} / {info.max_tokens:,} tokens ({info.remaining:,} remaining)"
    )


def format_context_breakdown(info: ContextInfo) -> str:
    """Format context usage breakdown by source.

    Args:
        info: Context usage info.

    Returns:
        Formatted breakdown string.
    """
    if not info.sources:
        return "No context sources tracked."

    lines = ["Context Breakdown:"]
    for source, tokens in sorted(
        info.sources.items(), key=lambda x: x[1], reverse=True
    ):
        pct = (tokens / max(info.used_tokens, 1)) * 100
        lines.append(f"  {source}: {tokens:,} tokens ({pct:.1f}%)")
    return "\n".join(lines)


def parse_context_command(text: str) -> dict[str, str]:
    """Parse a /context command.

    Subcommands:
    - /context — show usage bar
    - /context breakdown — show by-source breakdown
    - /context index — index codebase for RAG
    - /context retrieve <query> — retrieve relevant context

    Args:
        text: Command text after /context.

    Returns:
        Parsed command dict.
    """
    parts = text.strip().split(maxsplit=1)
    action = parts[0] if parts else "show"
    arg = parts[1] if len(parts) > 1 else ""
    return {"action": action, "argument": arg}
