"""General-purpose commands (help, search, telephone, version, etc.)."""

from __future__ import annotations

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands._base import SlashCommand

COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        spec=SlashCommandSpec(
            "/auto",
            "Toggle smart auto-mode: auto-approve safe tool calls, ask only for risky ops",
            "auto mode approve safe risky rules smart automatic",
            "general",
            available=True,
        ),
        handler_method="_handle_auto_command",
    ),
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
            "/persona",
            "Apply an output-style persona from .bog-agents/personas/*.md",
            "style voice tone persona output-style",
            "general",
            available=True,
        ),
        handler_method="_handle_persona_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/recipe",
            "Browse and install curated YAML recipe pipelines (review, audit, triage, …)",
            "pipeline recipe template yaml install registry workflow",
            "general",
            available=True,
        ),
        handler_method="_handle_recipe_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/teach",
            "Self-improving flywheel: propose new skills from this session, accept or reject",
            "skills learn flywheel propose accept memory teach",
            "general",
            available=True,
        ),
        handler_method="_handle_teach_command",
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
    # ---- Creative & exploration ---------------------------------------
    SlashCommand(
        spec=SlashCommandSpec(
            "/imagine",
            "Spawn N parallel subagents, each takes a different angle on the prompt",
            "parallel approaches variations explore ideate brainstorm options angles",
            "general",
            available=True,
            subcommands=(
                ("[N]", "Number of approaches to explore (default 3, max 6)"),
                ("[prompt]", "The problem to explore (defaults to last user message)"),
            ),
        ),
        handler_method="_handle_imagine_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/devil",
            "Critique the last assistant message with an adversarial second pass",
            "critique adversarial devils-advocate review challenge counter",
            "general",
            available=True,
        ),
        handler_method="_handle_devil_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/squad",
            "Multi-persona dialogue — security/perf/clarity personas debate code or design",
            "personas team review debate dialogue multi-agent",
            "general",
            available=True,
            subcommands=(
                ("review [target]", "Round-robin review of code/file/last-message"),
                ("list", "List configured personas (~/.bog-agents/squad.toml)"),
                ("init", "Create the default squad.toml with Alice/Bob/Carol"),
            ),
        ),
        handler_method="_handle_squad_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/dream",
            "Overnight ideation — daemon-driven exploration of TODOs and open issues",
            "ambient ideation overnight todos issues background nightly explore",
            "general",
            available=True,
            subcommands=(
                ("run", "Trigger a dream pass now (manual)"),
                ("config", "Show or edit the dream configuration"),
                ("list", "List recent dream files in ~/.bog-agents/dreams/"),
                ("install", "Install a daemon job that runs dreams nightly"),
            ),
        ),
        handler_method="_handle_dream_command",
    ),
    # ---- Productivity helpers -----------------------------------------
    SlashCommand(
        spec=SlashCommandSpec(
            "/scratch",
            "Ephemeral git worktrees with isolated venvs for safe experiments",
            "worktree experiment sandbox disposable ephemeral isolation",
            "general",
            available=True,
            subcommands=(
                ("new [label]", "Create a new scratch worktree"),
                ("list", "List active scratches"),
                ("enter <id>", "Switch the active working directory to a scratch"),
                ("drop <id>", "Delete a scratch worktree + venv"),
                ("drop --all", "Delete every scratch (asks for confirmation)"),
            ),
        ),
        handler_method="_handle_scratch_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/proxy",
            "Promote any shell command to an agent tool without writing an MCP server",
            "tool register shell command wrap mcp custom",
            "general",
            available=True,
            subcommands=(
                ("add", "Register a new shell-command tool"),
                ("list", "List registered proxy tools"),
                ("remove <name>", "Unregister a proxy tool"),
                ("show <name>", "Show the full definition of a proxy tool"),
            ),
        ),
        handler_method="_handle_proxy_command",
    ),
    # ---- SDLC unlocks --------------------------------------------------
    SlashCommand(
        spec=SlashCommandSpec(
            "/release-train",
            "Generate release notes, migration guide, and deprecation table from a tag",
            "release notes changelog migration deprecation upgrade-guide tag",
            "general",
            available=True,
            subcommands=(
                ("[tag]", "Generate notes for a specific tag (defaults to latest)"),
                ("[from]..[to]", "Generate notes for a range (e.g. v0.8.5..v0.8.6)"),
            ),
        ),
        handler_method="_handle_release_train_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/whisper",
            "Passive observation — agent watches file edits + git for a window, then synthesises",
            "passive observe watch background ambient synthesis",
            "general",
            available=True,
            subcommands=(
                ("start [minutes]", "Begin watching (default 30 minutes)"),
                ("stop", "Stop watching and emit the report"),
                ("status", "Show whisper state + current event count"),
                ("report", "Re-emit the last report"),
            ),
        ),
        handler_method="_handle_whisper_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/handoff",
            "Compile a handoff doc in the next dev's voice — what was tried, what's pending",
            "handoff context transfer summary teammate next-dev async remote",
            "general",
            available=True,
            subcommands=(
                ("[author]", "Name of the next dev (used as voice — optional)"),
            ),
        ),
        handler_method="_handle_handoff_command",
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
    # ---- Dreamscape (opt-in lifecycle + dreams + laws + imagination) ---
    SlashCommand(
        spec=SlashCommandSpec(
            "/agent-state",
            "Show lifecycle, imagination trait, recent dreams, and recent shared-memory entries",
            "lifecycle dormant dreaming imagination dashboard state observability",
            "general",
            available=True,
        ),
        handler_method="_handle_agent_state_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/repo",
            "One-screen summary of the current git checkout (branch, dirty files, top edits)",
            "repository git branch overview status clone",
            "general",
            available=True,
        ),
        handler_method="_handle_repo_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/dreamscape",
            "View or initialise the dreamscape configuration (lifecycle, dreams, laws, imagination)",
            "dreamscape config wizard preset master switch opt-in lifecycle imagination",
            "general",
            available=True,
            subcommands=(
                ("status", "Show current dreamscape config (default action)"),
                (
                    "init",
                    "Write a starter ~/.bog-agents/dreamscape.toml (master still off)",
                ),
                ("disable", "Force-disable the whole subsystem for this session"),
                (
                    "stats [H]",
                    "Show telemetry for last H hours (default 24, 'all' for full history)",
                ),
                (
                    "export [path]",
                    "Bundle telemetry across all agents into one JSON file (--no-metadata for privacy mode)",
                ),
            ),
        ),
        handler_method="_handle_dreamscape_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/laws",
            "Audit a sample against your Laws and Constitution, or initialise starter files",
            "laws constitution rules audit policy guardrails dreamscape",
            "general",
            available=True,
            subcommands=(
                ("audit <text>", "Dry-run the rules against a sample"),
                ("init", "Write starter laws.md + constitution.md (project-local)"),
                ("list", "Show currently loaded Laws + Constitution"),
                ("violations [N]", "Show the last N recorded violations (default 20)"),
            ),
        ),
        handler_method="_handle_laws_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/help-dream",
            "Last-ditch creative push — inject dream snippets into the next agent call",
            "imagination stuck unstuck dream creative help last-ditch",
            "general",
            available=True,
        ),
        handler_method="_handle_help_dream_command",
    ),
)
