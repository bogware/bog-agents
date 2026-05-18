"""CLI-side tests for the /expert wizard flow (Wave F2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.messages import AIMessage

if TYPE_CHECKING:
    from pathlib import Path


class _StubModel:
    """Pre-scripted chat model used by the wizard tests."""

    def __init__(self, scripted: str) -> None:
        self._scripted = scripted
        self.invocations: list = []

    def invoke(self, messages: list) -> Any:  # noqa: ANN401
        self.invocations.append(list(messages))
        return AIMessage(content=self._scripted)


@pytest.fixture(autouse=True)
def _isolated() -> None:
    from bog_agents_cli.expert_controller import reset_controllers

    reset_controllers()


class TestWizardControllerWiring:
    def test_wizard_no_args_prints_menu(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(tmp_path)
        out = c.wizard("")
        assert "safety" in out
        assert "budget" in out
        assert "Usage: /expert wizard" in out

    def test_wizard_category_only_prints_help(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        c = get_controller(tmp_path)
        out = c.wizard("safety")
        assert "Safety" in out

    def test_wizard_with_intent_builds_proposal(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import get_controller

        yaml = (
            "- name: block_rm\n"
            "  when:\n"
            "    - tool_call:\n"
            "        command:\n"
            "          matches: '^rm '\n"
            "  then:\n"
            "    - deny: 'no rm'\n"
        )
        c = get_controller(tmp_path, model_factory=lambda: _StubModel(yaml))
        out = c.wizard("safety block rm commands")
        assert "Wizard" in out
        assert "block_rm" in out

    def test_wizard_dispatch_via_slash(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch

        out = dispatch("/expert wizard", tmp_path)
        assert "safety" in out
        assert "budget" in out

    def test_wizard_proposal_can_be_saved(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import dispatch, get_controller

        yaml = (
            "- name: wiz_safety_rule\n"
            "  when:\n"
            "    - tool_call:\n"
            "        command:\n"
            "          matches: '^rm '\n"
            "  then:\n"
            "    - deny: 'wiz blocked'\n"
        )
        c = get_controller(tmp_path, model_factory=lambda: _StubModel(yaml))
        c.wizard("safety block rm commands")
        # Proposal was stashed — saving it through the existing write flow.
        save_out = dispatch("/expert write save", tmp_path)
        assert "Saved" in save_out
        rules_dir = tmp_path / ".bog-agents" / "expert_rules"
        files = list(rules_dir.glob("*.yaml"))
        assert len(files) == 1
        assert "wiz_safety_rule" in files[0].read_text(encoding="utf-8")
