"""ROADMAP #72: the CLI switch for code mode and its restricted-profile exclusion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bog_agents_cli.agent import _compliance_features
from bog_agents_cli.config_manifest import resolve_option
from bog_agents_cli.trust_profiles import RESTRICTED_TOOL_NAMES, restricted_profile

if TYPE_CHECKING:
    import pytest


def test_option_and_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOG_AGENTS_CODE_MODE", raising=False)
    assert resolve_option("tools.code_mode") is False
    assert "enable_code_mode" not in _compliance_features("agent")
    monkeypatch.setenv("BOG_AGENTS_CODE_MODE", "1")
    assert resolve_option("tools.code_mode") is True
    assert _compliance_features("agent")["enable_code_mode"] is True
    assert "enable_code_mode" not in _compliance_features("agent", restricted=True)


def test_restricted_profile_strips_code_mode() -> None:
    assert {"run_code", "execute_mcp_script"} <= RESTRICTED_TOOL_NAMES
    profile = restricted_profile()
    assert profile.tool_excluded("run_code") and profile.tool_excluded(
        "execute_mcp_script"
    )
