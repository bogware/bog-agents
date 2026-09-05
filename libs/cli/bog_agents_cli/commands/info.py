"""Session and informational commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/changelog",
            "Open the project changelog in your browser",
            "release notes",
            "info",
            available=True,
        ),
        handler_method="_handle_reference_url_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/checkpoint",
            "Save, load, and list named session checkpoints for easy resume",
            "save restore resume snapshot session history",
            "info",
            available=True,
        ),
        handler_method="_handle_checkpoint_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/docs",
            "Open documentation and project guides",
            "readme api",
            "info",
            available=True,
        ),
        handler_method="_handle_reference_url_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/init",
            "Generate `AGENTS.md` for the current repository",
            "setup agents onboard",
            "info",
            available=True,
        ),
        handler_method="_dispatch_init_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/onboard",
            "Start an interactive codebase onboarding guide; `/onboard import <tool>` brings past sessions in",
            "tour walkthrough new import sessions claude codex cline migrate",
            "info",
            available=True,
        ),
        handler_method="_dispatch_onboard_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/rewind",
            "Browse checkpoints and fork a thread from an earlier snapshot",
            "checkpoint recover restore history",
            "info",
            available=True,
        ),
        handler_method="_handle_rewind_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/session",
            "Show or update session label, tags, project, summary, and exports",
            "name duration info metadata",
            "info",
            available=True,
        ),
        handler_method="_handle_session_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/tokens",
            "Show current token usage and context breakdown",
            "cost context window budget spend",
            "info",
            aliases=("/cost", "/context"),
            available=True,
        ),
        handler_method="_handle_tokens_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/trace",
            "Open the current thread in LangSmith",
            "langsmith observability",
            "info",
            available=True,
        ),
        handler_method="_handle_trace_command",
    ),
)
