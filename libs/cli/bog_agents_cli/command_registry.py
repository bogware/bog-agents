"""Central slash-command registry for bog-agents-cli.

This module is intentionally lightweight so it can be imported from startup
paths like help rendering, autocomplete wiring, and command-palette helpers
without pulling in the SDK or Textual runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches


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
        "Create or switch conversation branches",
        "fork explore",
        "git",
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
    ),
    SlashCommandSpec(
        "/extensions",
        "Manage extensions (list/install/uninstall)",
        "plugins",
        "config",
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
    ),
    SlashCommandSpec(
        "/plugin",
        "Manage plugins (list/install/uninstall/create)",
        "marketplace skills",
        "config",
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
    ),
    SlashCommandSpec(
        "/profile",
        "Switch configuration profile",
        "config preset",
        "config",
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
    ),
    SlashCommandSpec(
        "/resume",
        "Resume a saved thread or browse thread history",
        "continue switch history",
        "info",
        available=True,
    ),
    SlashCommandSpec(
        "/replay",
        "Replay agent actions for debugging",
        "debug trace",
        "general",
    ),
    SlashCommandSpec(
        "/resolve",
        "AI-assisted merge conflict resolution",
        "conflict merge",
        "git",
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
        "Show session details or assign a local session name",
        "name duration info",
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
        "Undo last file change (git checkpoint)",
        "revert rollback",
        "git",
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
    ),
)

FEATURED_HELP_COMMANDS_LEFT: tuple[str, ...] = (
    "/help",
    "/commands",
    "/model",
    "/compact",
    "/resume",
    "/threads",
    "/session",
    "/permissions",
    "/keybindings",
    "/skills",
)

FEATURED_HELP_COMMANDS_RIGHT: tuple[str, ...] = (
    "/mcp",
    "/trace",
    "/tokens",
    "/background",
    "/dashboard",
    "/review",
    "/recommend",
    "/reload",
    "/clear",
    "/quit",
)

_SPEC_BY_NAME: dict[str, SlashCommandSpec] = {
    spec.name: spec for spec in SLASH_COMMAND_SPECS
}
_SPEC_BY_ALIAS: dict[str, SlashCommandSpec] = {
    alias: spec for spec in SLASH_COMMAND_SPECS for alias in spec.aliases
}


def _select_specs(
    *, include_unavailable: bool = False
) -> tuple[SlashCommandSpec, ...]:
    """Return the subset of command specs to expose to users."""
    if include_unavailable:
        return SLASH_COMMAND_SPECS
    return tuple(spec for spec in SLASH_COMMAND_SPECS if spec.available)


def get_registered_command_names(
    *, include_aliases: bool = False, include_unavailable: bool = False
) -> list[str]:
    """Return registered command names for the requested command surface."""
    names = [
        spec.name
        for spec in _select_specs(include_unavailable=include_unavailable)
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
    spec = _SPEC_BY_NAME.get(normalized) or _SPEC_BY_ALIAS.get(normalized)
    if spec is None:
        return None
    if not include_unavailable and not spec.available:
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
            cutoff=0.45,
        )
        deduped = {close_lookup[name].name: close_lookup[name] for name in close}
        matches = [(10, spec) for spec in deduped.values()]

    matches.sort(key=lambda item: (-item[0], item[1].name))
    return [spec for _score, spec in matches[:limit]]
