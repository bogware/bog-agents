"""Tests for the `/sweep` controller and its attachment to the CLI agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from bog_agents.middleware.street_sweeper import StreetSweeperMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from bog_agents_cli.sweep_controller import (
    SweepController,
    get_sweep_controller,
    reset_sweep_controllers,
)


def _make_fake_chat_model() -> GenericFakeChatModel:
    """A fake chat model that satisfies the summarization middleware build."""
    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    model.profile = {"max_input_tokens": 200000}
    return model


def test_controller_starts_disabled_and_toggles() -> None:
    reset_sweep_controllers()
    controller = get_sweep_controller(".")
    assert controller.middleware.enabled is False

    msg_on = controller.handle_sweep("on")
    assert controller.middleware.enabled is True
    assert "ON" in msg_on

    msg_off = controller.handle_sweep("off")
    assert controller.middleware.enabled is False
    assert "OFF" in msg_off


def test_controller_aggressive_toggle() -> None:
    reset_sweep_controllers()
    controller = get_sweep_controller(".")
    assert controller.middleware.aggressive is True
    controller.handle_sweep("aggressive off")
    assert controller.middleware.aggressive is False
    controller.handle_sweep("aggressive on")
    assert controller.middleware.aggressive is True


def test_controller_status_and_log_are_safe_when_empty() -> None:
    reset_sweep_controllers()
    controller = get_sweep_controller(".")
    assert "Street sweeper" in controller.handle_sweep("status")
    assert "no actions" in controller.handle_sweep("log").lower()
    assert "Usage" in controller.handle_sweep("bogus")


def test_lifetime_ledger_persists_and_reloads(tmp_path: Path) -> None:
    """Per-call deltas accumulate into a ledger that survives a new controller."""
    ledger = tmp_path / "ledger.json"
    controller = SweepController(ledger_path=ledger)

    # Simulate two swept model calls firing the on_commit hook.
    controller.middleware.on_commit(
        {"tokens_saved": 500, "dollars_saved": 0.0015, "actions": 3}
    )
    controller.middleware.on_commit(
        {"tokens_saved": 250, "dollars_saved": 0.0007, "actions": 1}
    )

    assert ledger.exists()
    status = controller.handle_sweep("status")
    assert "Lifetime" in status
    assert "750" in status  # 500 + 250 tokens

    # A fresh controller pointed at the same ledger reloads the running total.
    reloaded = SweepController(ledger_path=ledger)
    assert int(reloaded._lifetime["tokens_saved"]) == 750
    assert reloaded._lifetime["calls"] == 2

    # Reset zeros it on disk too.
    reloaded.handle_sweep("reset")
    assert int(reloaded._lifetime["tokens_saved"]) == 0
    assert SweepController(ledger_path=ledger)._lifetime["tokens_saved"] == 0


def test_singleton_is_per_cwd() -> None:
    reset_sweep_controllers()
    a = get_sweep_controller(".")
    b = get_sweep_controller(".")
    assert a is b


def test_create_cli_agent_attaches_disabled_sweeper(tmp_path: Path) -> None:
    """`create_cli_agent` attaches the controller's sweeper, pointed at the backend."""
    reset_sweep_controllers()
    from bog_agents_cli.agent import create_cli_agent

    fake_model = _make_fake_chat_model()
    captured: dict[str, object] = {}

    def _capture_create_agent(**kwargs: object) -> Mock:
        captured.update(kwargs)
        return Mock()

    with (
        patch("bog_agents_cli.agent.LocalShellBackend"),
        patch("bog_agents_cli.agent.FilesystemBackend"),
        patch("bog_agents_cli.agent.create_agent", side_effect=_capture_create_agent),
        patch(
            "bog_agents_cli.config.create_model", return_value=Mock(model=fake_model)
        ),
        patch("bog_agents_cli.agent.get_system_prompt", return_value=""),
    ):
        create_cli_agent(
            model="fake-model",
            assistant_id="test",
            enable_memory=False,
            enable_skills=False,
            enable_shell=False,
            interactive=False,
            cwd=str(tmp_path),
        )

    middleware = captured.get("middleware") or []
    sweepers = [m for m in middleware if isinstance(m, StreetSweeperMiddleware)]
    assert len(sweepers) == 1
    # Same instance the controller owns, and disabled by default (opt-in).
    assert sweepers[0] is get_sweep_controller(tmp_path).middleware
    assert sweepers[0].enabled is False
    assert sweepers[0]._backend is not None  # backend was attached
