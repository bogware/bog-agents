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
            subcommands=(
                ("list", "List recent threads"),
                ("delete", "Delete a thread (usage: /threads delete <id>)"),
                ("resume", "Resume a specific thread (usage: /threads resume <id>)"),
                ("search", "Search thread history (usage: /threads search <query>)"),
                (
                    "group",
                    "List threads grouped by branch/PR (usage: /threads group pr [all])",
                ),
                ("archive", "Archive a thread (usage: /threads archive <id>)"),
                ("unarchive", "Un-archive a thread (usage: /threads unarchive <id>)"),
                ("unread", "Mark a thread unread (usage: /threads unread <id>)"),
                ("read", "Mark a thread read (usage: /threads read <id>)"),
            ),
        ),
        handler_method="_handle_threads_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/recap",
            "Where this session stands: turns, spend, files, running work, what needs you, your /btw notes",
            "summary status catch-up waiting notes",
            "info",
            available=True,
        ),
        handler_method="_handle_recap_command",
    ),
)
