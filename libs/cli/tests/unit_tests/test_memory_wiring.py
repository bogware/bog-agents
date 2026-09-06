"""ROADMAP #75 wiring: the headless `memory` twin and the advisor tool registration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

STORE = """## Agent-Recorded Memories
<!-- bog-agents auto-memories: written by the agent via the `remember` tool. Safe to edit, reorganize, or delete. -->

- (note) one
- (note) one
"""


def test_headless_memory_twin_is_model_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents_cli import headless_commands as hc

    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text(STORE, encoding="utf-8")
    handler = hc.HEADLESS_COMMANDS["memory"][1]
    result = handler("rebuild")
    assert (
        result.ok and "Memory rebuild (dedup)" in result.text and "2 → 1" in result.text
    )
    assert handler("status").ok and "Candidate pending" in handler("status").text
    assert (
        handler("apply").ok
        and (tmp_path / "AGENTS.md").read_text(encoding="utf-8").count("(note) one")
        == 1
    )
    assert not handler("apply").ok and not handler("dance").ok


def test_advisor_tool_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents.cost_ledger import CostLedger

    from bog_agents_cli import agent as agent_mod, operator_mode

    monkeypatch.setattr(
        operator_mode, "operator_config_path", lambda: tmp_path / "missing.toml"
    )
    monkeypatch.delenv("BOG_AGENTS_ADVISOR", raising=False)
    assert (
        agent_mod._advisor_tools("anthropic:claude-haiku-4-5", None, restricted=False)
        == []
    )
    monkeypatch.setenv("BOG_AGENTS_ADVISOR", "1")
    monkeypatch.setenv("BOG_AGENTS_ADVISOR_MAX_QUESTIONS", "2")
    ledger = CostLedger()
    tools = agent_mod._advisor_tools(
        "anthropic:claude-haiku-4-5", ledger, restricted=False
    )
    assert [t.name for t in tools] == ["ask_advisor"]
    assert (
        agent_mod._advisor_tools("anthropic:claude-haiku-4-5", ledger, restricted=True)
        == []
    )
    hard = operator_mode.resolve_tiers(operator_mode.load_operator_config())[
        "hard"
    ].model
    assert agent_mod._advisor_tools(hard, ledger, restricted=False) == []
