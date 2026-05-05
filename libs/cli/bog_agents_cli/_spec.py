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
    """Metadata for one slash command.

    ``subcommands`` is a tuple of ``(name, description)`` pairs the
    autocomplete layer offers after the user types a space following the
    command. Empty for commands that don't take subcommands.
    """

    name: str
    description: str
    hidden_keywords: str = ""
    category: str = "general"
    shortcut: str = ""
    aliases: tuple[str, ...] = ()
    available: bool = False
    subcommands: tuple[tuple[str, str], ...] = ()


__all__ = ["SlashCommandSpec"]
