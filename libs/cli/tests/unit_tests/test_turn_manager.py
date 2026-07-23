"""Unit tests for the turn-lifecycle coordinator (v4 CLI-CORE-1/-4)."""

from __future__ import annotations

from bog_agents_cli.turn_manager import TurnManager


def test_starts_idle() -> None:
    turns = TurnManager()
    assert turns.agent_running is False
    assert turns.agent_worker is None
    assert turns.shell_running is False
    assert turns.busy is False


def test_begin_agent_sets_flag_and_worker_together() -> None:
    turns = TurnManager()
    sentinel = object()

    turns.begin_agent(sentinel)  # type: ignore[arg-type]

    assert turns.agent_running is True
    assert turns.agent_worker is sentinel
    assert turns.busy is True


def test_end_agent_clears_flag_and_worker() -> None:
    turns = TurnManager()
    turns.begin_agent(object())  # type: ignore[arg-type]

    turns.end_agent()

    assert turns.agent_running is False
    assert turns.agent_worker is None
    assert turns.busy is False


def test_shell_toggles_busy_independently() -> None:
    turns = TurnManager()

    turns.begin_shell()
    assert turns.shell_running is True
    # busy is the single definition of "in flight" — shell counts even with no
    # agent turn running.
    assert turns.busy is True
    assert turns.agent_running is False

    turns.end_shell()
    assert turns.shell_running is False
    assert turns.busy is False


def test_busy_reflects_either_lane() -> None:
    turns = TurnManager()
    turns.begin_agent(object())  # type: ignore[arg-type]
    turns.begin_shell()
    assert turns.busy is True

    # Ending only the agent lane leaves shell busy.
    turns.end_agent()
    assert turns.busy is True

    turns.end_shell()
    assert turns.busy is False
