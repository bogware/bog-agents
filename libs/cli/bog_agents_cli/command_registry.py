"""Central slash-command registry for bog-agents-cli.

This module is intentionally lightweight so it can be imported from startup
paths like help rendering, autocomplete wiring, and command-palette helpers
without pulling in the SDK or Textual runtime.

Spec data is sourced from the modular ``bog_agents_cli/commands/`` package.
Adding a new slash command means adding one module under ``commands/``;
this file exposes the search / palette / autocomplete helpers that build
on top of that data plus extension-contributed commands loaded at runtime.
"""

from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path

from bog_agents_cli._spec import SlashCommandSpec
from bog_agents_cli.commands import COMMANDS as _COMMANDS

SLASH_COMMAND_SPECS: tuple[SlashCommandSpec, ...] = tuple(c.spec for c in _COMMANDS)


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
    "/qa",
    "/peat",
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
    "/record",
    "/replay",
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
