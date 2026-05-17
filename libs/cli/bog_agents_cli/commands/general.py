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
            "/expert",
            "Expert Mode — forward+backward chaining rule engine for policy gates, "
            "deterministic constraints, and deny/modify/approval actions",
            "rules engine policy expert clips forward backward chaining inference",
            "general",
            available=True,
            subcommands=(
                ("on|off", "Toggle the engine"),
                ("list", "List loaded rules"),
                ("show <name>", "Show a single rule's contents"),
                ("lint", "Static analysis of the rulebook"),
                ("trace [N]", "Last engine-run trace (default 50 entries)"),
                ("memory", "Working-memory contents"),
                ("assert <fact_type> k=v ...", "Inject a fact"),
                ("dry-run <fact_type> k=v ...", "Simulate firing without persisting"),
                ("write <intent>", "LLM generates a rule from your description"),
                ("write save [name]", "Commit the most recent /expert write proposal"),
                ("write cancel", "Discard the pending /expert write proposal"),
                ("wizard", "Guided setup — show category menu"),
                ("wizard <category> <intent>", "Guided rule authoring per category"),
                ("watch", "Show scheduled-proposer status"),
                ("watch start [N] [--apply]", "Start the scheduled proposer"),
                ("watch stop", "Stop the scheduled proposer"),
                ("propose [agent]", "Mine dreams + history → stage a proposed rule"),
                ("propose [agent] --apply", "Mine dreams + apply the rule directly (skip staging)"),
                ("proposals", "List pending dream-mined proposals"),
                ("proposals approve <name>", "Promote a proposal to active rules"),
                ("proposals discard <name>", "Delete a pending proposal"),
                ("run", "Run the engine to a fixed point"),
                ("reload", "Reload rules from disk"),
                ("example", "Print a starter rule YAML"),
            ),
        ),
        handler_method="_handle_expert_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/why",
            "Backward-chain explanation — which rules can produce this fact and "
            "are their conditions satisfied?",
            "explain trace reason proof backward expert why",
            "general",
            available=True,
            subcommands=(
                ("<fact_type> [k=v ...]", "Fact-type pattern to explain"),
            ),
        ),
        handler_method="_handle_why_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/sidecar",
            "Ask a one-shot question in a fresh read-only subagent — without "
            "disturbing the main agent's work-in-progress",
            "question ask isolated subagent read-only sidecar parallel side aside",
            "general",
            available=True,
            subcommands=(
                ("<question>", "What you want the sidecar to answer"),
            ),
        ),
        handler_method="_handle_sidecar_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/orchestrate",
            "Decompose a goal into mode-typed subtasks and run each in its "
            "own read-only worker; results boomerang back as a tree summary",
            "orchestrate plan decompose boomerang subtasks roo workers fanout",
            "general",
            available=True,
            subcommands=(
                ("<goal>", "Plain-English goal to decompose and run"),
                ("--parallel <goal>", "Run subtasks concurrently (each gets a fresh model)"),
            ),
        ),
        handler_method="_handle_orchestrate_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/prove",
            "Backward-chain query — could the engine derive this goal from current working memory?",
            "prove goal derive backward chain target",
            "general",
            available=True,
            subcommands=(
                ("<fact_type> [k=v ...]", "Goal pattern to prove"),
            ),
        ),
        handler_method="_handle_prove_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/browser",
            "Computer Use — Playwright-backed browser tools (navigate, click, screenshot, eval)",
            "browser computer use playwright chromium navigate click screenshot",
            "general",
            available=True,
            subcommands=(
                ("", "Show browser session status"),
                ("close", "Close the active browser session"),
            ),
        ),
        handler_method="_handle_browser_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/web",
            "Fetch a URL and add the cleaned page content to the conversation as context",
            "url fetch http get page web scrape document docs reference",
            "general",
            available=True,
            subcommands=(
                ("<url>", "Fetch URL and inject as context"),
                ("<url> -- <question>", "Fetch URL then ask the agent <question> about it"),
            ),
        ),
        handler_method="_handle_web_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/tracefile",
            "TraceFile v1 — export, import, verify content-addressed signed "
            "traces. Designed as an open format other agent CLIs can read + write.",
            "tracefile export import verify ed25519 merkle signed portable causal",
            "general",
            available=True,
            subcommands=(
                ("export <session-id|latest>", "Sign + write a TraceFile"),
                ("import <path>", "Verify and render a TraceFile"),
                ("verify <path>", "Verify only; no render"),
                ("keygen [--out PATH]", "Mint an Ed25519 keypair"),
                ("help", "Usage"),
            ),
        ),
        handler_method="_handle_tracefile_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/compliance",
            "Compliance auditor — run a YAML audit pack against the loaded "
            "rules + causal traces; produces a signed SOC2-aligned report",
            "audit compliance soc2 iso nist controls invariant evidence report seal pack",
            "general",
            available=True,
            subcommands=(
                ("run <pack>", "Run an audit pack (project-local or bundled)"),
                ("list", "List saved audit reports newest-first"),
                ("show <filename>", "Read a saved report; verifies the seal"),
                ("packs", "List available audit packs"),
                ("help", "Usage + YAML schema"),
            ),
        ),
        handler_method="_handle_compliance_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/postmortem",
            "Causal-replay postmortem — analyse a session, propose remediations "
            "(rule / skill / config)",
            "postmortem analyse review trace causal remediation rule skill",
            "general",
            available=True,
            subcommands=(
                ("<session-id>", "Analyse a specific causal session"),
                ("latest", "Newest session"),
                ("latest <note>", "Add free-text context for the model"),
                ("list", "List saved postmortems"),
            ),
        ),
        handler_method="_handle_postmortem_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/prove-invariant",
            "Formally prove a safety invariant against the loaded expert rules "
            "(e.g. 'no PII tool may run after git push to main')",
            "prove invariant formal verification z3 smt policy guarantee safety",
            "general",
            available=True,
            subcommands=(
                ("<path.yaml>", "Prove an invariant from a YAML file"),
                ("list", "List invariants/*.yaml in this project"),
                ("--z3", "Prefer the Z3-backed prover (optional flag)"),
                ("help", "Show usage + YAML schema"),
            ),
        ),
        handler_method="_handle_prove_invariant_command",
    ),
    SlashCommand(
        spec=SlashCommandSpec(
            "/trace-mind",
            "Trace-mind — see the proof tree behind any agent decision, "
            "tool call, or final answer (was: /causal)",
            "trace mind causal replay debug provenance ancestry why-did graph history",
            "general",
            available=True,
            subcommands=(
                ("", "Show recording state + counts"),
                ("on|off", "Toggle recording for this cwd"),
                ("last [N]", "Show the last N events (default 20)"),
                ("why <event_id>", "Render the causal-ancestry tree of one event"),
                ("graph [N]", "Render the whole session as a tree"),
                ("sessions [id]", "List recorded sessions (or render one by id)"),
                ("replay <id> [flags]", "Time-travel rule replay (Q3)"),
            ),
        ),
        handler_method="_handle_trace_mind_command",
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
                (
                    "config",
                    "Show enrichment config + which transport each source resolved to",
                ),
                ("enable jira|halo", "Turn an enrichment source ON (persists to TOML)"),
                ("disable jira|halo", "Turn an enrichment source OFF"),
                ("test jira|halo", "Probe the configured transport for the source"),
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
                (
                    "enable [--session] [--with imagination]",
                    "Turn dreamscape ON with sensible defaults — persists to TOML; --session = env-var-only",
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
