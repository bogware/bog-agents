"""Regression tests for the modular ``bog_agents_cli/commands/`` registry.

The ``commands/`` package is the new home for slash-command metadata. For
phase 1 it lives alongside the legacy ``_COMMAND_HANDLER_NAMES`` map on
``BogAgentsApp``. These tests assert that the registry is well-formed and
that anything declared there actually maps to a real handler method.
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


def test_registry_does_not_collide_with_legacy_map() -> None:
    """When a command is migrated, registry + legacy map must agree.

    The legacy map can keep its entry as a fallback during the rollout,
    but the two must point at the same handler method.
    """
    for slash_name, handler_name in COMMAND_HANDLER_MAP.items():
        legacy = BogAgentsApp._COMMAND_HANDLER_NAMES.get(slash_name)
        if legacy is None:
            continue
        assert legacy == handler_name, (
            f"Conflict for {slash_name}: registry={handler_name}, legacy={legacy}"
        )


def test_session_module_exports_clear_resume_threads() -> None:
    """Ensure the migrated session module is wired up."""
    names = {c.name for c in COMMANDS}
    assert {"/clear", "/resume", "/threads"}.issubset(names)
