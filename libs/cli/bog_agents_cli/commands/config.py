"""Configuration, settings, and feature-toggle commands."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/compact",
            "Summarize conversation to reduce context usage",
            "retain keep drop summarize",
            "config",
            available=True,
        ),
        handler_method="_handle_compact_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/compress",
            "Intelligent context compression — auto-compact with ratio report and progress",
            "compact context window tokens summarize reduce packing",
            "config",
            available=True,
        ),
        handler_method="_handle_compress_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/doctor",
            "Run health check diagnostics for the local CLI environment",
            "check status",
            "config",
            available=True,
        ),
        handler_method="_handle_doctor_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/effort",
            "Set effort level (low/medium/high/max)",
            "quality speed",
            "config",
            available=True,
        ),
        handler_method="_handle_effort_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/extensions",
            "Manage extensions and extensibility packages",
            "plugins marketplace",
            "config",
            available=True,
        ),
        handler_method="_handle_plugin_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/keybindings",
            "Show current keybindings or the config file path",
            "keys shortcuts",
            "config",
            available=True,
        ),
        handler_method="_handle_keybindings_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/logs",
            "Show the log file path and recent warnings or errors",
            "debug trace errors",
            "config",
            available=True,
        ),
        handler_method="_dispatch_logs_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/mcp",
            "MCP server marketplace — browse, install, and manage servers (jira, terraform, github…)",
            "servers tools install registry catalog marketplace plugin integration",
            "config",
            available=True,
        ),
        handler_method="_handle_mcp_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/model",
            "Switch models or manage the default model",
            "provider swap ollama",
            "config",
            available=True,
        ),
        handler_method="_handle_model_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/permissions",
            "Show approval mode and shell permission settings",
            "safety approvals shell trust",
            "config",
            available=True,
        ),
        handler_method="_handle_permissions_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/plan",
            "Toggle read-only plan mode",
            "readonly architect",
            "config",
            available=True,
        ),
        handler_method="_handle_plan_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/plugin",
            "Manage plugins and extensions — includes Claude Code skill import and MCP sync",
            "marketplace skills extensions claude-code mcp sync import compatible",
            "config",
            available=True,
        ),
        handler_method="_handle_plugin_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/profile",
            "Switch configuration profile",
            "config preset",
            "config",
            available=True,
        ),
        handler_method="_handle_profile_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/reload",
            "Reload config from environment variables and `.env`",
            "refresh",
            "config",
            available=True,
        ),
        handler_method="_handle_reload_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/settings",
            "Configure providers, models, and fallbacks",
            "config preferences setup",
            "config",
            available=True,
        ),
        handler_method="_handle_settings_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/skills",
            "Show loaded skills and their search paths",
            "abilities memory",
            "config",
            available=True,
        ),
        handler_method="_handle_skills_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/vars",
            "Manage secrets and variables (API keys, URLs, tokens)",
            "secrets env config keys tokens credentials",
            "config",
            available=True,
        ),
        handler_method="_handle_vars_command",
    ),
)
