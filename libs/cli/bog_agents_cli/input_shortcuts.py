"""Input shortcut prefixes for quick actions.

Feature #27: Shell prefix `!` — quick shell escape from the input prompt.
Feature #28: Memory prefix `#` — quick append to AGENTS.md from the prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class InputAction:
    """Result of parsing an input shortcut."""

    action_type: str
    """Type: 'shell', 'memory', 'message' (normal)."""

    content: str
    """The processed content."""

    original: str
    """Original input text."""


def parse_input_shortcuts(text: str) -> InputAction:
    """Parse input text for shortcut prefixes.

    Supported prefixes:
    - `!command` — execute shell command directly
    - `# note` — append note to AGENTS.md memory

    Args:
        text: Raw input text.

    Returns:
        InputAction describing the parsed result.
    """
    stripped = text.strip()

    if stripped.startswith("!") and len(stripped) > 1:
        return InputAction(
            action_type="shell",
            content=stripped[1:].strip(),
            original=text,
        )

    if stripped.startswith("#") and len(stripped) > 1 and not stripped.startswith("##"):
        return InputAction(
            action_type="memory",
            content=stripped[1:].strip(),
            original=text,
        )

    return InputAction(
        action_type="message",
        content=text,
        original=text,
    )


def append_to_memory(agent_md_path: Path, note: str) -> bool:
    """Append a note to the AGENTS.md memory file.

    Args:
        agent_md_path: Path to the AGENTS.md file.
        note: Note text to append.

    Returns:
        True if successfully appended.
    """
    try:
        existing = ""
        if agent_md_path.exists():
            existing = agent_md_path.read_text(encoding="utf-8")

        separator = (
            "\n\n"
            if existing and not existing.endswith("\n\n")
            else "\n"
            if existing and not existing.endswith("\n")
            else ""
        )
        agent_md_path.write_text(
            f"{existing}{separator}## Memory Note\n\n{note}\n", encoding="utf-8"
        )
        logger.info("Appended memory note to %s", agent_md_path)
        return True
    except OSError as e:
        logger.warning("Failed to append to %s: %s", agent_md_path, e)
        return False
