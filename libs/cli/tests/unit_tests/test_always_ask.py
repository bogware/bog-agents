"""Tests for the ``--always-ask`` paranoid-mode flag."""

from __future__ import annotations

from bog_agents_cli.app import TextualSessionState


def test_session_state_default_always_ask_off() -> None:
    state = TextualSessionState()
    assert state.always_ask is False
    assert state.auto_approve is False


def test_session_state_independently_carries_always_ask() -> None:
    state = TextualSessionState(auto_approve=True, always_ask=True)
    # The two flags are independent; always_ask is meant to OVERRIDE
    # auto_approve at the call site, not silently turn it off.
    assert state.auto_approve is True
    assert state.always_ask is True


def test_command_registry_includes_always_ask() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/always-ask" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/always-ask"] == "_handle_always_ask_command"


def test_telephone_command_is_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/telephone" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/telephone"] == "_handle_telephone_command"


async def test_non_interactive_rejects_always_ask() -> None:
    """Non-interactive mode must refuse always-ask (would deadlock)."""
    from bog_agents_cli.non_interactive import run_non_interactive

    code = await run_non_interactive(
        message="hello",
        always_ask=True,
        quiet=True,
    )
    assert code == 2
