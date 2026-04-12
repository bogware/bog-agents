"""Central slash-command registry for bog-agents-cli.

This module is intentionally lightweight so it can be imported from startup
paths like help rendering, autocomplete wiring, and command-palette helpers
without pulling in the SDK or Textual runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    """Metadata for one slash command."""

    name: str
    description: str
    hidden_keywords: str = ""
    category: str = "general"
    shortcut: str = ""
    aliases: tuple[str, ...] = ()
    available: bool = False


SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec(
        "/help",
        "Show slash command help and search by keyword",
        "commands reference",
        "general",
        "?",
        available=True,
    ),
    SlashCommandSpec(
        "/agent",
        "Manage parallel agent threads (list/spawn/switch/stop)",
        "thread multi parallel",
        "agent",
        available=True,
    ),
    SlashCommandSpec(
        "/api",
        "Send API requests and test endpoints",
        "http rest curl",
        "web",
    ),
    SlashCommandSpec(
        "/audit",
        "Audit dependencies for vulnerabilities",
        "security deps",
        "quality",
        available=True,
    ),
    SlashCommandSpec(
        "/background",
        "Manage background agent tasks (run/list/cancel/status)",
        "bg task async",
        "agent",
        available=True,
    ),
    SlashCommandSpec(
        "/branch",
        "Manage git branches for local workflows",
        "git checkout switch create",
        "git",
        available=True,
    ),
    SlashCommandSpec(
        "/changelog",
        "Open the project changelog in your browser",
        "release notes",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/clear",
        "Clear chat history and start a fresh thread",
        "reset new conversation",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/commands",
        "Browse available slash commands and quick descriptions",
        "help reference discover",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/compact",
        "Summarize conversation to reduce context usage",
        "retain keep drop summarize",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/dashboard",
        "Show the multi-agent dashboard with status and costs",
        "agents monitor panel",
        "agent",
        available=True,
    ),
    SlashCommandSpec(
        "/diff",
        "Show pending file changes as a diff",
        "changes git",
        "git",
        available=True,
    ),
    SlashCommandSpec(
        "/docs",
        "Open documentation and project guides",
        "readme api",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/doctor",
        "Run health check diagnostics for the local CLI environment",
        "check status",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/effort",
        "Set effort level (low/medium/high/max)",
        "quality speed",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/extensions",
        "Manage extensions and extensibility packages",
        "plugins marketplace",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/feedback",
        "Open the issue tracker to report a bug or request a feature",
        "bug issue request",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/health",
        "Codebase health score and analysis",
        "quality complexity coverage",
        "analysis",
        available=True,
    ),
    SlashCommandSpec(
        "/image",
        "Analyze images or paste from clipboard",
        "screenshot multimodal",
        "multimodal",
    ),
    SlashCommandSpec(
        "/init",
        "Generate `AGENTS.md` for the current repository",
        "setup agents onboard",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/infra",
        "Generate infrastructure code (Docker/K8s/Terraform)",
        "devops deploy",
        "analysis",
        available=True,
    ),
    SlashCommandSpec(
        "/keybindings",
        "Show current keybindings or the config file path",
        "keys shortcuts",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/logs",
        "Show the log file path and recent warnings or errors",
        "debug trace errors",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/mcp",
        "Show active MCP servers and tools",
        "servers tools",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/migrate",
        "Plan technology migration",
        "upgrade convert",
        "analysis",
        available=True,
    ),
    SlashCommandSpec(
        "/model",
        "Switch models or manage the default model",
        "provider swap ollama",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/model-route",
        "Configure automatic model routing",
        "auto cost optimize",
        "config",
    ),
    SlashCommandSpec(
        "/onboard",
        "Start an interactive codebase onboarding guide",
        "tour walkthrough new",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/permissions",
        "Show approval mode and shell permission settings",
        "safety approvals shell trust",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/plan",
        "Toggle read-only plan mode",
        "readonly architect",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/plugin",
        "Manage plugins and extensions (list/info/install/enable/disable)",
        "marketplace skills extensions",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/pr",
        "Pull request management (create/list/review)",
        "github merge",
        "git",
    ),
    SlashCommandSpec(
        "/preview",
        "Start or stop local dev server preview",
        "serve browser",
        "web",
        available=True,
    ),
    SlashCommandSpec(
        "/profile",
        "Switch configuration profile",
        "config preset",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/quit",
        "Exit the app",
        "close leave",
        "general",
        aliases=("/q",),
        available=True,
    ),
    SlashCommandSpec(
        "/recommend",
        "Run AI-powered code review and recommendation flows",
        "review audit advise persona focus",
        "quality",
        available=True,
    ),
    SlashCommandSpec(
        "/record",
        "Start or stop recording session for replay",
        "capture",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/reload",
        "Reload config from environment variables and `.env`",
        "refresh",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/remember",
        "Update memory and skills from the current conversation",
        "memory skills capture",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/remote",
        "Submit a task for remote or cloud execution",
        "cloud",
        "web",
        available=True,
    ),
    SlashCommandSpec(
        "/rewind",
        "Browse checkpoints and fork a thread from an earlier snapshot",
        "checkpoint recover restore history",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/resume",
        "Resume a saved thread by id, tag, project, or browse history",
        "continue switch history recover",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/replay",
        "Replay agent actions for debugging",
        "debug trace",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/resolve",
        "AI-assisted merge conflict resolution",
        "conflict merge",
        "git",
        available=True,
    ),
    SlashCommandSpec(
        "/review",
        "Ask the agent for a structured code review",
        "lint check staged files commit",
        "quality",
        available=True,
    ),
    SlashCommandSpec(
        "/session",
        "Show or update session label, tags, project, summary, and exports",
        "name duration info metadata",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/settings",
        "Configure providers, models, and fallbacks",
        "config preferences setup",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/skills",
        "Show loaded skills and their search paths",
        "abilities memory",
        "config",
        available=True,
    ),
    SlashCommandSpec(
        "/teach",
        "Start teaching mode to learn a workflow",
        "learn skill",
        "general",
    ),
    SlashCommandSpec(
        "/team",
        "Team settings and roles management",
        "enterprise org",
        "enterprise",
    ),
    SlashCommandSpec(
        "/test",
        "Run tests with coverage and generate test skeletons",
        "coverage pytest",
        "quality",
        available=True,
    ),
    SlashCommandSpec(
        "/threads",
        "Browse and resume previous threads",
        "continue history sessions",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/tokens",
        "Show current token usage and context breakdown",
        "cost context window budget spend",
        "info",
        aliases=("/cost", "/context"),
        available=True,
    ),
    SlashCommandSpec(
        "/trace",
        "Open the current thread in LangSmith",
        "langsmith observability",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/undo",
        "Inspect or restore tracked file changes with git",
        "revert rollback",
        "git",
        available=True,
    ),
    SlashCommandSpec(
        "/version",
        "Show CLI and SDK versions",
        "build release",
        "general",
        available=True,
    ),
    SlashCommandSpec(
        "/worktree",
        "Manage git worktrees for isolated work",
        "isolate parallel",
        "git",
        available=True,
    ),
)

FEATURED_HELP_COMMANDS_LEFT: tuple[str, ...] = (
    "/help",
    "/commands",
    "/model",
    "/profile",
    "/plan",
    "/effort",
    "/compact",
    "/resume",
    "/threads",
    "/session",
    "/permissions",
)

FEATURED_HELP_COMMANDS_RIGHT: tuple[str, ...] = (
    "/diff",
    "/worktree",
    "/agent",
    "/mcp",
    "/trace",
    "/tokens",
    "/background",
    "/plugin",
    "/remote",
    "/review",
    "/quit",
)


def _load_dynamic_extension_specs() -> tuple[SlashCommandSpec, ...]:
    """Load slash-command specs contributed by enabled extensions."""
    try:
        from bog_agents_cli.extensibility import get_extension_commands

        commands = get_extension_commands(Path.home() / ".bog-agents")
    except Exception:
        return ()

    specs: list[SlashCommandSpec] = []
    for command in commands:
        specs.append(
            SlashCommandSpec(
                command.name,
                command.description,
                f"extension {command.extension_name} {command.hidden_keywords}".strip(),
                "extension",
                aliases=command.aliases,
                available=True,
            )
        )
    return tuple(specs)


def _select_specs(*, include_unavailable: bool = False) -> tuple[SlashCommandSpec, ...]:
    """Return the subset of command specs to expose to users."""
    combined: list[SlashCommandSpec] = list(SLASH_COMMAND_SPECS)
    known = {spec.name for spec in combined}
    known_aliases = {
        alias for spec in combined for alias in spec.aliases if isinstance(alias, str)
    }
    for spec in _load_dynamic_extension_specs():
        if spec.name in known or spec.name in known_aliases:
            continue
        combined.append(spec)
    if include_unavailable:
        return tuple(combined)
    return tuple(spec for spec in combined if spec.available)


def _lookup_maps(
    *, include_unavailable: bool = False
) -> tuple[dict[str, SlashCommandSpec], dict[str, SlashCommandSpec]]:
    specs = _select_specs(include_unavailable=include_unavailable)
    by_name = {spec.name: spec for spec in specs}
    by_alias = {alias: spec for spec in specs for alias in spec.aliases}
    return by_name, by_alias


def get_registered_command_names(
    *, include_aliases: bool = False, include_unavailable: bool = False
) -> list[str]:
    """Return registered command names for the requested command surface."""
    names = [
        spec.name for spec in _select_specs(include_unavailable=include_unavailable)
    ]
    if include_aliases:
        for spec in _select_specs(include_unavailable=include_unavailable):
            names.extend(spec.aliases)
    return names


def get_slash_commands(
    *, include_unavailable: bool = False
) -> list[tuple[str, str, str]]:
    """Return autocomplete-friendly slash command tuples."""
    return [
        (spec.name, spec.description, spec.hidden_keywords)
        for spec in _select_specs(include_unavailable=include_unavailable)
    ]


def get_command_palette_specs(
    *, include_unavailable: bool = False
) -> list[SlashCommandSpec]:
    """Return command specs suitable for palette and search views."""
    return list(_select_specs(include_unavailable=include_unavailable))


def get_command_spec(
    name: str, *, include_unavailable: bool = False
) -> SlashCommandSpec | None:
    """Look up one command spec by slash name or alias."""
    normalized = name.strip().lower()
    by_name, by_alias = _lookup_maps(include_unavailable=include_unavailable)
    spec = by_name.get(normalized) or by_alias.get(normalized)
    if spec is None:
        return None
    return spec


def describe_commands(
    command_names: tuple[str, ...] | list[str],
) -> list[tuple[str, str]]:
    """Return `(name, description)` pairs for known commands."""
    pairs: list[tuple[str, str]] = []
    for name in command_names:
        spec = get_command_spec(name)
        if spec is not None:
            pairs.append((spec.name, spec.description))
    return pairs


def search_slash_commands(
    query: str, *, limit: int = 8, include_unavailable: bool = False
) -> list[SlashCommandSpec]:
    """Search slash commands with fuzzy matching."""
    specs = _select_specs(include_unavailable=include_unavailable)
    normalized = query.lower().strip().lstrip("/")
    if not normalized:
        return list(specs[:limit])

    matches: list[tuple[int, SlashCommandSpec]] = []
    for spec in specs:
        names = [
            spec.name.lstrip("/"),
            *(alias.lstrip("/") for alias in spec.aliases),
        ]
        score = 0
        if normalized in names:
            score += 100
        if any(name.startswith(normalized) for name in names):
            score += 60
        elif any(normalized in name for name in names):
            score += 40
        if normalized in spec.description.lower():
            score += 20
        if normalized and any(
            normalized in keyword for keyword in spec.hidden_keywords.lower().split()
        ):
            score += 15
        if score:
            matches.append((score, spec))

    if not matches:
        close_lookup = {
            spec_name: spec
            for spec in specs
            for spec_name in [
                spec.name.lstrip("/"),
                *(alias.lstrip("/") for alias in spec.aliases),
            ]
        }
        close = get_close_matches(
            normalized,
            list(close_lookup),
            n=limit,
            cutoff=0.6,
        )
        deduped = {close_lookup[name].name: close_lookup[name] for name in close}
        matches = [(10, spec) for spec in deduped.values()]

    matches.sort(key=lambda item: (-item[0], item[1].name))
    return [spec for _score, spec in matches[:limit]]
