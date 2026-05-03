"""Base class for individual slash-command modules.

A ``SlashCommand`` ties together everything the rest of the CLI needs to
know about one command in a single object:

* ``spec`` — the ``SlashCommandSpec`` that drives autocomplete and ``/help``.
* ``handler_method`` — the bound method name on ``BogAgentsApp`` that
  implements the command. We keep this as a string instead of a callable
  so the ``commands/`` package can be imported without importing the
  giant ``app.py`` (avoids an import cycle).

Eventually individual ``SlashCommand`` instances will own their handlers
directly, replacing the ``_handle_*_command`` methods on ``BogAgentsApp``.
For phase 1 we keep the existing methods and use the registry as a
declarative SHIM.
"""

from __future__ import annotations

from dataclasses import dataclass

from bog_agents_cli._spec import SlashCommandSpec


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """One slash command, plus the info needed to dispatch + advertise it."""

    spec: SlashCommandSpec
    handler_method: str

    @property
    def name(self) -> str:
        """Slash form of the command, e.g. ``/clear``."""
        return self.spec.name


__all__ = ["SlashCommand", "SlashCommandSpec"]
