"""Session-related slash commands: /clear, /resume, /threads.

Each module under ``bog_agents_cli/commands/`` exports a ``COMMANDS``
tuple. The package-level registry imports them on package load.
"""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/clear",
            "Clear chat history and start a fresh thread",
            "reset new conversation",
            "general",
            available=True,
        ),
        handler_method="_handle_clear_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/resume",
            "Resume a saved thread by id, tag, project, or browse history",
            "continue switch history recover",
            "info",
            available=True,
        ),
        handler_method="_handle_resume_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/threads",
            "Browse and resume previous threads",
            "continue history sessions",
            "info",
            available=True,
        ),
        handler_method="_handle_threads_command",
    ),
)
