"""Regression: every available slash command must have a runnable handler.

After A1 phase 2 the legacy ``BogAgentsApp._COMMAND_HANDLER_NAMES`` map is
gone — the modular ``bog_agents_cli/commands/`` package is the single
source of truth. These tests catch drift between the spec list and the
handler methods on ``BogAgentsApp`` so adding a new command means editing
exactly one file under ``commands/``.
"""

from __future__ import annotations


def test_every_available_spec_has_a_dispatcher_entry() -> None:
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    missing: list[str] = []
    for spec in SLASH_COMMAND_SPECS:
        if not spec.available:
            continue
        if spec.name not in COMMAND_HANDLER_MAP:
            missing.append(spec.name)
    assert not missing, f"Available slash commands with no dispatcher entry: {missing}"


def test_every_dispatcher_entry_maps_to_an_existing_method() -> None:
    from bog_agents_cli.app import BogAgentsApp
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    missing: list[str] = []
    for command, handler_name in COMMAND_HANDLER_MAP.items():
        if not hasattr(BogAgentsApp, handler_name):
            missing.append(f"{command} -> {handler_name}")
    assert not missing, f"Dispatcher targets a non-existent method: {missing}"


def test_unavailable_specs_carry_no_handler() -> None:
    """``available=False`` specs are forward-looking placeholders.

    They have a SlashCommandSpec for visibility in the registry but should
    NOT be wired into COMMAND_HANDLER_MAP — otherwise autocomplete would
    advertise something that doesn't actually run.
    """
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    for spec in SLASH_COMMAND_SPECS:
        if spec.available:
            continue
        assert spec.name not in COMMAND_HANDLER_MAP, (
            f"{spec.name} is marked available=False but has a registered handler"
        )
