"""Selective context compaction with user control.

Feature #4: /compact with selective retention — user-controlled compaction
where users can specify what to keep vs. drop from context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CompactRule:
    """A rule for selective compaction."""

    action: str
    """'keep' or 'drop'."""

    pattern: str
    """What to match: 'tool_results', 'code_blocks', 'errors', 'all_before:<n>'."""

    description: str = ""
    """Human-readable description of what this rule does."""


@dataclass
class CompactConfig:
    """Configuration for selective compaction."""

    rules: list[CompactRule] = field(default_factory=list)
    """Ordered rules for what to keep/drop."""

    keep_last_n: int = 5
    """Always keep the last N messages."""

    keep_errors: bool = True
    """Always keep error messages."""

    keep_user_messages: bool = True
    """Always keep user messages."""

    summary_model: str = ""
    """Model to use for summarization (empty = use current)."""


# Predefined compaction strategies
COMPACT_STRATEGIES: dict[str, CompactConfig] = {
    "aggressive": CompactConfig(
        rules=[
            CompactRule("drop", "tool_results", "Drop all tool results"),
            CompactRule("keep", "errors", "Keep error messages"),
        ],
        keep_last_n=3,
        keep_errors=True,
    ),
    "moderate": CompactConfig(
        rules=[
            CompactRule("drop", "large_tool_results", "Drop tool results > 500 chars"),
            CompactRule("keep", "code_blocks", "Keep code blocks"),
        ],
        keep_last_n=5,
        keep_errors=True,
    ),
    "minimal": CompactConfig(
        rules=[],
        keep_last_n=10,
        keep_errors=True,
    ),
}


def parse_compact_args(args: str) -> CompactConfig:
    """Parse /compact command arguments.

    Supports:
    - `/compact` — default moderate strategy
    - `/compact aggressive` — aggressive strategy
    - `/compact keep:errors drop:tool_results` — custom rules
    - `/compact last:3` — keep only last 3 messages

    Args:
        args: Command arguments string.

    Returns:
        CompactConfig based on the parsed arguments.
    """
    args = args.strip()

    if not args:
        return COMPACT_STRATEGIES["moderate"]

    if args in COMPACT_STRATEGIES:
        return COMPACT_STRATEGIES[args]

    # Parse custom rules
    config = CompactConfig()
    parts = args.split()

    for part in parts:
        if part.startswith("keep:"):
            pattern = part[5:]
            config.rules.append(CompactRule("keep", pattern))
        elif part.startswith("drop:"):
            pattern = part[5:]
            config.rules.append(CompactRule("drop", pattern))
        elif part.startswith("last:"):
            try:
                config.keep_last_n = int(part[5:])
            except ValueError:
                logger.warning("Invalid last:N value: %s", part)

    return config


def should_keep_message(
    message: dict[str, Any],
    index: int,
    total: int,
    config: CompactConfig,
) -> bool:
    """Determine if a message should be kept during compaction.

    Args:
        message: Message dict with 'role', 'content', etc.
        index: Message index in the conversation.
        total: Total number of messages.
        config: Compaction configuration.

    Returns:
        True if the message should be kept.
    """
    # Always keep the last N messages
    if index >= total - config.keep_last_n:
        return True

    role = message.get("role", "")
    content = str(message.get("content", ""))

    # Always keep user messages if configured
    if config.keep_user_messages and role == "user":
        return True

    # Always keep errors if configured
    if config.keep_errors and ("error" in content.lower() or "Error" in content):
        return True

    # Apply custom rules (first match wins)
    for rule in config.rules:
        if _matches_pattern(message, rule.pattern):
            return rule.action == "keep"

    # Default: drop (it will be summarized)
    return False


def _matches_pattern(message: dict[str, Any], pattern: str) -> bool:
    """Check if a message matches a compaction pattern.

    Args:
        message: Message dict.
        pattern: Pattern to match.

    Returns:
        True if the message matches.
    """
    content = str(message.get("content", ""))
    role = message.get("role", "")

    if pattern == "tool_results" and role == "tool":
        return True
    if pattern == "large_tool_results" and role == "tool" and len(content) > 500:
        return True
    if pattern == "code_blocks" and "```" in content:
        return True
    if pattern == "errors" and ("error" in content.lower() or "Error" in content):
        return True
    if pattern == "ai_messages" and role == "assistant":
        return True
    return False


def format_compact_summary(kept: int, dropped: int, strategy: str) -> str:
    """Format a summary of the compaction result.

    Args:
        kept: Number of messages kept.
        dropped: Number of messages dropped/summarized.
        strategy: Strategy name used.

    Returns:
        Formatted summary string.
    """
    return (
        f"Compacted conversation: kept {kept} messages, "
        f"summarized {dropped} messages (strategy: {strategy})"
    )
