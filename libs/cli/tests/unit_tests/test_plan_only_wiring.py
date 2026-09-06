"""ROADMAP #69 wiring: `plan_only` through the server config and the agent builder, and `--plan` plan-then-execute."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli._server_config import ServerConfig
from bog_agents_cli._server_constants import ENV_PREFIX

if TYPE_CHECKING:
    import pytest


def test_server_config_round_trips_plan_only(monkeypatch: pytest.MonkeyPatch) -> None:
    config = ServerConfig.from_cli_args(
        project_context=None,
        assistant_id="agent",
        model_name="anthropic:claude-haiku-4-5",
        model_params=None,
        auto_approve=True,
        sandbox_type=None,
        sandbox_id=None,
        sandbox_setup=None,
        enable_shell=False,
        enable_ask_user=False,
        plan_only=True,
        mcp_config_path=None,
        no_mcp=True,
        trust_project_mcp=None,
        interactive=False,
    )
    env = config.to_env()
    assert env["PLAN_ONLY"] == "true"
    for suffix, value in env.items():
        if value is not None:
            monkeypatch.setenv(f"{ENV_PREFIX}{suffix}", value)
        else:
            monkeypatch.delenv(f"{ENV_PREFIX}{suffix}", raising=False)
    assert ServerConfig.from_env().plan_only is True
    monkeypatch.delenv(f"{ENV_PREFIX}PLAN_ONLY", raising=False)
    assert ServerConfig.from_env().plan_only is False


def test_plan_only_agent_has_no_mutating_tools(tmp_path: Path) -> None:
    from bog_agents.middleware.plan_mode import MUTATING_TOOLS
    from bog_agents.token_audit import audit_agent

    from bog_agents_cli.agent import create_cli_agent

    def _build(model: object) -> object:
        return create_cli_agent(
            model=model,  # type: ignore[arg-type]
            assistant_id="agent",
            auto_approve=True,
            cwd=tmp_path,
            plan_only=True,
        )

    names = {t.name for t in audit_agent(_build, method="approx").tools}
    assert names and not names & MUTATING_TOOLS
    assert "read_file" in names and "execute" not in names and "write_file" not in names


def test_run_plan_then_execute_two_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from bog_agents_cli import non_interactive as ni

    calls: list[dict[str, Any]] = []

    async def _fake(message: str, **kwargs: Any) -> int:
        calls.append({"message": message, **kwargs})
        sink = kwargs.get("sink")
        if sink is not None:
            sink.append("# Plan\n1. Read x\n2. Patch y\n")
        return 0

    monkeypatch.setattr(ni, "run_non_interactive", _fake)
    code = asyncio.run(
        ni.run_plan_then_execute(
            "fix the parser",
            execute=True,
            quiet=True,
            assistant_id="agent",
            stream=True,
            plan_mode=True,
        )
    )
    assert code == 0 and len(calls) == 2
    planning, execution = calls
    assert (
        planning["plan_only"] is True
        and planning["auto_approve"] is True
        and planning["stream"] is False
        and "fix the parser" in planning["message"]
    )
    assert "plan_mode" not in planning and planning["assistant_id"] == "agent"
    assert (
        execution["message"].startswith("Execute this approved plan")
        and "2. Patch y" in execution["message"]
    )
    assert (
        execution["auto_mode"] is True
        and execution["auto_approve"] is False
        and "sink" not in execution
    )

    calls.clear()
    assert (
        asyncio.run(ni.run_plan_then_execute("x", execute=False, quiet=True)) == 0
        and len(calls) == 1
    )

    async def _empty(message: str, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(ni, "run_non_interactive", _empty)
    assert asyncio.run(ni.run_plan_then_execute("x", execute=True, quiet=True)) == 1


def test_plan_flag_rides_the_non_interactive_path() -> None:
    from bog_agents_cli.main import parse_args

    saved = os.sys.argv[:]  # type: ignore[attr-defined]
    try:
        os.sys.argv[:] = ["bog-agents", "--plan", "ship it", "--auto"]  # type: ignore[attr-defined]
        args = parse_args()
    finally:
        os.sys.argv[:] = saved  # type: ignore[attr-defined]
    assert args.plan_prompt == "ship it" and args.auto_mode is True
