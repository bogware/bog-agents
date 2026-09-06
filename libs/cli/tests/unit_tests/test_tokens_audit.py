"""ROADMAP #54 (CLI): `--mini`, ServerConfig.harness_profile, `/tokens middleware` and its headless twin."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli._server_config import ServerConfig
from bog_agents_cli.agent import MINI_KEEP_TOOLS
from bog_agents_cli.headless_commands import HEADLESS_COMMANDS
from bog_agents_cli.main import parse_args
from bog_agents_cli.tokens_audit_controller import audit_cli_agent, render_cli_audit

if TYPE_CHECKING:
    import pytest


def test_mini_flag_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["bog-agents", "--mini"])
    assert parse_args().mini is True
    monkeypatch.setattr(sys, "argv", ["bog-agents"])
    assert parse_args().mini is False


def test_server_config_round_trips_harness_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = ServerConfig(harness_profile="lean").to_env()
    assert env["HARNESS_PROFILE"] == "lean"
    assert ServerConfig().to_env()["HARNESS_PROFILE"] is None
    for suffix, value in env.items():
        if value is None:
            monkeypatch.delenv(f"DA_SERVER_{suffix}", raising=False)
        else:
            monkeypatch.setenv(f"DA_SERVER_{suffix}", value)
    assert ServerConfig.from_env().harness_profile == "lean"
    monkeypatch.setenv("DA_SERVER_HARNESS_PROFILE", "")
    assert ServerConfig.from_env().harness_profile is None


def test_cli_audit_default_vs_lean_and_headless_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    default = audit_cli_agent(harness_profile=None, cwd=tmp_path, method="approx")
    lean = audit_cli_agent(harness_profile="lean", cwd=tmp_path, method="approx")
    assert default.per_turn_overhead > lean.per_turn_overhead > 0
    assert "write_todos" not in {t.name for t in lean.tools}
    lean_names = {t.name for t in lean.tools}
    assert {"tool_search", "select", "read_file", "execute"} <= lean_names
    assert len(lean_names) <= len(MINI_KEEP_TOOLS) + 2, sorted(lean_names)
    assert len(default.tools) > 3 * len(lean_names)
    names = {m.name for m in default.middleware}
    assert "FilesystemMiddleware" in names and len(names) > 5, names
    text = render_cli_audit(default, harness_profile=None)
    assert text.startswith("Profile: default") and "Harness overhead:" in text
    assert render_cli_audit(lean, harness_profile="lean").startswith("Profile: lean")

    handler = HEADLESS_COMMANDS["tokens"][1]
    result = handler("middleware --mini")
    assert result.ok and result.text.startswith("Profile: lean")
    assert result.data["per_turn_overhead"] > 0
    assert HEADLESS_COMMANDS["tokens"][1]("bogus").ok is False
