"""Tests for the modular ``bog_agents_cli/commands/`` registry.

The ``commands/`` package is the only source of truth for slash-command
dispatch and autocomplete. These tests catch drift between command
modules, the aggregated registry, and the underlying handler methods on
``BogAgentsApp``.
"""

from __future__ import annotations

from bog_agents_cli.app import BogAgentsApp
from bog_agents_cli.commands import COMMAND_HANDLER_MAP, COMMANDS


def test_every_command_has_unique_name() -> None:
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names)), f"Duplicate slash names in registry: {names}"


def test_every_command_handler_is_real_method() -> None:
    missing: list[str] = []
    for command in COMMANDS:
        if not hasattr(BogAgentsApp, command.handler_method):
            missing.append(f"{command.name} -> {command.handler_method}")
    assert not missing, f"commands/ registry points at missing methods: {missing}"


def test_registry_handler_map_includes_aliases() -> None:
    """When a command declares aliases, both forms map to the same handler."""
    for command in COMMANDS:
        for alias in command.spec.aliases:
            assert COMMAND_HANDLER_MAP.get(alias) == command.handler_method, (
                f"alias {alias} of {command.name} is missing or maps wrong"
            )


def test_session_module_exports_clear_resume_threads() -> None:
    """The migrated session module must be wired up."""
    names = {c.name for c in COMMANDS}
    assert {"/clear", "/resume", "/threads"}.issubset(names)


def test_registry_resolution_count_matches_legacy_surface() -> None:
    """Sanity check: the new registry covers ~78 dispatch entries (75 specs + 3 aliases)."""
    assert len(COMMANDS) >= 70
    # Aliases bump the handler-map size.
    assert len(COMMAND_HANDLER_MAP) >= len(COMMANDS)


def test_handler_map_resolves_alias_q_for_quit() -> None:
    """``/q`` is an alias of ``/quit`` — both should dispatch to the same method."""
    assert COMMAND_HANDLER_MAP.get("/quit") == COMMAND_HANDLER_MAP.get("/q")


def test_handler_map_resolves_alias_cost_and_context_for_tokens() -> None:
    """``/cost`` and ``/context`` are aliases of ``/tokens``."""
    target = COMMAND_HANDLER_MAP.get("/tokens")
    assert target is not None
    assert COMMAND_HANDLER_MAP.get("/cost") == target
    assert COMMAND_HANDLER_MAP.get("/context") == target
