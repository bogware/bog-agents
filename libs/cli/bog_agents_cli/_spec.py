"""Shared slash-command spec dataclass.

Lives in its own module so both ``command_registry`` (which exposes the
legacy ``SLASH_COMMAND_SPECS`` aggregate plus search helpers) and the
modular ``commands/`` package (the new home for individual command
declarations) can import it without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    """Metadata for one slash command."""

    name: str
    description: str
    hidden_keywords: str = ""
    category: str = "general"
    shortcut: str = ""
    aliases: tuple[str, ...] = ()
    available: bool = False


__all__ = ["SlashCommandSpec"]
