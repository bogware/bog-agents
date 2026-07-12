"""UI / display preference commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/silent",
            "Quiet mode: tool calls show as one-liners (full details still in log)",
            "quiet collapse mute hide",
            "ui",
            available=True,
        ),
        handler_method="_handle_silent_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/verbose",
            "Verbose mode: tool calls show as expandable widgets (default)",
            "loud expand show",
            "ui",
            available=True,
        ),
        handler_method="_handle_verbose_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/always-ask",
            "Toggle paranoid mode: every tool call requires approval (overrides auto-approve)",
            "approval safe paranoid review confirm hitl",
            "ui",
            available=True,
        ),
        handler_method="_handle_always_ask_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/theme",
            "Change the color theme: /theme opens the picker, /theme <name> switches, /theme list",
            "color palette appearance dark light scheme skin",
            "ui",
            available=True,
            subcommands=(("list", "List available themes"),),
        ),
        handler_method="_handle_theme_command",
    ),
)
