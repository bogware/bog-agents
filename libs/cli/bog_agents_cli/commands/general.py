"""General-purpose commands (help, search, telephone, version, etc.)."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/build",
            "Interactive wizard — create skills, prompts, and pipelines step by step",
            "wizard create new template scaffold variablize builder",
            "general",
            available=True,
        ),
        handler_method="_handle_build_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/telephone",
            "Rewrite a casual prompt as a production-grade LLM prompt before submitting",
            "rewrite improve clarify polish prompt-engineering",
            "general",
            available=True,
        ),
        handler_method="_handle_telephone_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/commands",
            "Browse available slash commands and quick descriptions",
            "help reference discover",
            "general",
            available=True,
        ),
        handler_method="_handle_help_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/explain",
            "Deep-dive explanation of any symbol, file, or concept in the codebase",
            "docs understand symbol function class architecture why",
            "general",
            available=True,
        ),
        handler_method="_handle_explain_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/feedback",
            "Open the issue tracker to report a bug or request a feature",
            "bug issue request",
            "general",
            available=True,
        ),
        handler_method="_handle_reference_url_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/help",
            "Show slash command help and search by keyword",
            "commands reference",
            "general",
            shortcut="?",
            available=True,
        ),
        handler_method="_handle_help_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/index",
            "Build and search a local knowledge-base index of the codebase",
            "search knowledge base symbol tfidf find query",
            "general",
            available=True,
        ),
        handler_method="_handle_index_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/pipeline",
            "Run a saved pipeline (chained prompts, skills, slash commands)",
            "workflow chain schedule cron automate steps yaml",
            "general",
            available=True,
        ),
        handler_method="_handle_pipeline_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/prompt",
            "Browse and run saved prompts with variable substitution",
            "template library saved custom variable",
            "general",
            available=True,
        ),
        handler_method="_handle_prompt_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/quit",
            "Exit the app",
            "close leave",
            "general",
            aliases=("/q",),
            available=True,
        ),
        handler_method="_handle_quit_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/record",
            "Start or stop recording session for replay",
            "capture",
            "general",
            available=True,
        ),
        handler_method="_handle_record_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/remember",
            "Update memory and skills from the current conversation",
            "memory skills capture",
            "general",
            available=True,
        ),
        handler_method="_handle_remember_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/replay",
            "Replay agent actions for debugging",
            "debug trace",
            "general",
            available=True,
        ),
        handler_method="_handle_replay_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/rules",
            "Manage project rules (.bog-agents/rules/) — auto-inject contextual guidelines",
            "mdc cursor-rules guidelines standards glob always inject frontmatter",
            "general",
            available=True,
        ),
        handler_method="_handle_rules_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/search",
            "Hybrid codebase search — ripgrep exact + fuzzy filename + semantic",
            "ripgrep rg find grep vector embeddings semantic hybrid",
            "general",
            available=True,
        ),
        handler_method="_handle_search_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/think",
            "Enable extended thinking / deep reasoning on the next query",
            "reasoning cot chain-of-thought extended-thinking budget tokens anthropic gemini",
            "general",
            available=True,
        ),
        handler_method="_handle_think_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/version",
            "Show CLI and SDK versions",
            "build release",
            "general",
            available=True,
        ),
        handler_method="_handle_version_command",
    ),
)
