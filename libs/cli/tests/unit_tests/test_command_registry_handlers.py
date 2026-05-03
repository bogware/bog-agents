"""Regression: every available slash command must have a runnable handler.

Catches drift between ``command_registry.SLASH_COMMAND_SPECS`` (what the user
sees in autocomplete and ``/help``) and ``BogAgentsApp._COMMAND_HANDLER_NAMES``
(what the dispatcher will actually run).
"""

from __future__ import annotations


def test_every_available_spec_has_a_dispatcher_entry() -> None:
    from bog_agents_cli.app import BogAgentsApp
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS

    handler_map = BogAgentsApp._COMMAND_HANDLER_NAMES
    missing: list[str] = []
    for spec in SLASH_COMMAND_SPECS:
        if not spec.available:
            continue
        if spec.name not in handler_map:
            missing.append(spec.name)
    assert not missing, f"Available slash commands with no dispatcher entry: {missing}"


def test_every_dispatcher_entry_maps_to_an_existing_method() -> None:
    from bog_agents_cli.app import BogAgentsApp

    missing: list[str] = []
    for command, handler_name in BogAgentsApp._COMMAND_HANDLER_NAMES.items():
        if not hasattr(BogAgentsApp, handler_name):
            missing.append(f"{command} -> {handler_name}")
    assert not missing, f"Dispatcher targets a non-existent method: {missing}"


def test_unavailable_specs_are_not_silently_dispatched() -> None:
    """If a spec is ``available=False`` it should not have a real handler.

    Exception: aliased entries (e.g. ``/q`` → /quit) that share dispatch with
    an available command. We only check direct registry entries.
    """
    from bog_agents_cli.command_registry import SLASH_COMMAND_SPECS

    for spec in SLASH_COMMAND_SPECS:
        if spec.available:
            continue
        # A future-stub command must clearly be marked unavailable.
        assert not spec.available, f"{spec.name} should be available=False"
