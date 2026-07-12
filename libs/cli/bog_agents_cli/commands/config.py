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
            "/bedrock",
            "Probe AWS Bedrock connectivity — credentials, region, model access, inference",
            "aws connection test diagnostic credentials region",
            "config",
            available=True,
            subcommands=(
                ("test", "Run the Bedrock connection probe"),
                ("status", "Same as `test` — quick view of credentials + region"),
                (
                    "fix",
                    "Probe + show one copy-paste command per failure (set region, sso login, request model access, …)",
                ),
                (
                    "config",
                    "Show active Bedrock settings (auth mode, profile, region, config path)",
                ),
            ),
        ),
        handler_method="_handle_bedrock_command",
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
            "Set native reasoning effort (per-model levels)",
            "quality speed reasoning thinking none low medium high xhigh max",
            "config",
            available=True,
            subcommands=(
                ("none", "Reasoning off (where supported)"),
                ("low", "Minimal reasoning overhead"),
                ("medium", "Balanced reasoning and speed"),
                ("high", "Thorough analysis (default)"),
                ("xhigh", "Extended reasoning (where supported)"),
                ("max", "Maximum reasoning depth (where supported)"),
            ),
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
            subcommands=(
                (
                    "marketplace",
                    "Browse the full server catalog (35+ servers, all categories)",
                ),
                ("featured", "Curated quick-pick list (jira, github, aws, …)"),
                ("list", "List configured servers in ~/.bog-agents/.mcp.json"),
                (
                    "search",
                    "Search the catalog by keyword (usage: /mcp search <query>)",
                ),
                (
                    "install",
                    "Install a server from the catalog (usage: /mcp install <id>)",
                ),
                ("info", "Show catalog entry details (usage: /mcp info <id>)"),
                ("add", "Add a custom stdio server (usage: /mcp add <name> <cmd> ...)"),
                ("remove", "Remove a configured server (usage: /mcp remove <name>)"),
                ("login", "Sign in to an OAuth server (usage: /mcp login <server>)"),
                ("logout", "Remove stored OAuth tokens (usage: /mcp logout <server>)"),
                ("status", "Show OAuth login status for configured servers"),
                ("trust", "Manage project stdio server trust"),
                ("view", "Open the live viewer (configured servers + tools)"),
                ("help", "Show /mcp usage"),
            ),
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
            subcommands=(
                ("list", "Show installed plugins"),
                ("install", "Install a plugin (usage: /plugin install <name>)"),
                ("remove", "Remove a plugin (usage: /plugin remove <name>)"),
                ("sync", "Sync MCP configs between .mcp.json and Claude Desktop"),
                ("import", "Import Claude Code-compatible skills from .claude/"),
            ),
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
            "/refresh-models",
            "Re-scan installed providers and rebuild the model catalog",
            "models providers catalog refresh ollama bedrock anthropic openai",
            "config",
            available=True,
        ),
        handler_method="_handle_refresh_models_command",
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
            "/smoketest",
            "Test model+provider connectivity — credentials, network, tiny inference call",
            "test ping verify smoke connection auth inference thinking bedrock",
            "config",
            available=True,
            subcommands=(
                ("[provider:model]", "Test a specific model spec (defaults to active)"),
                ("--thinking", "Also exercise extended-thinking parameter support"),
            ),
        ),
        handler_method="_handle_smoketest_command",
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
