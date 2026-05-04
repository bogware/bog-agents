"""Slash-command registry — single source of truth for dispatch + autocomplete.

Each command lives in its own module under ``bog_agents_cli/commands/``.
At import time we walk the directory and assemble ``COMMANDS`` (ordered
``SlashCommand`` tuple) and ``COMMAND_HANDLER_MAP`` (slash → method name,
including aliases).

Adding a new slash command means adding *one* module under ``commands/``;
``command_registry.SLASH_COMMAND_SPECS`` and ``app._COMMAND_HANDLER_NAMES``
no longer need lockstep edits.
"""

from __future__ import annotations

from bog_agents_cli.commands._base import SlashCommand
from bog_agents_cli.commands._registry import discover as _discover

_COMMANDS, COMMAND_HANDLER_MAP = _discover()  # noqa: RUF067  # populated at import time
COMMANDS: tuple[SlashCommand, ...] = _COMMANDS  # noqa: RUF067  # public alias for the discovered tuple

__all__ = [
    "COMMANDS",
    "COMMAND_HANDLER_MAP",
    "SlashCommand",
]
