"""Web and remote-execution commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/preview",
            "Start or stop local dev server preview",
            "serve browser",
            "web",
            available=True,
        ),
        handler_method="_handle_preview_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/remote",
            "Submit a task for remote or cloud execution",
            "cloud",
            "web",
            available=True,
        ),
        handler_method="_handle_remote_command",
    ),
)
