"""BOG_AGENTS_MCP_TRUST allow-override for project MCP trust (CT-1 / v4).

The variable was defined, exposed in the config manifest, and printed in the
non-TTY deny message, but read nowhere — so a CI user following the printed
instruction was silently denied. These tests pin that it is now honored.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import bog_agents_cli.main as main_mod
from bog_agents_cli import _env_vars
from bog_agents_cli.main import _check_mcp_project_trust, _mcp_trust_env_override


class TestEnvOverrideHelper:
    def test_unset_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_env_vars.MCP_TRUST, raising=False)
        assert _mcp_trust_env_override() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(_env_vars.MCP_TRUST, value)
        assert _mcp_trust_env_override() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", ""])
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(_env_vars.MCP_TRUST, value)
        assert _mcp_trust_env_override() is False


def _stub_project_with_stdio(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Make _check_mcp_project_trust see one project stdio server to gate."""
    from bog_agents_cli import mcp_tools, project_utils

    cfg_path = tmp_path / ".mcp.json"
    monkeypatch.setattr(
        project_utils.ProjectContext,
        "from_user_cwd",
        classmethod(
            lambda cls, _cwd: SimpleNamespace(project_root=tmp_path, user_cwd=tmp_path)
        ),
    )
    monkeypatch.setattr(mcp_tools, "discover_mcp_configs", lambda **_: [cfg_path])
    monkeypatch.setattr(
        mcp_tools, "classify_discovered_configs", lambda _paths: ([], [cfg_path])
    )
    monkeypatch.setattr(
        mcp_tools, "load_mcp_config_lenient", lambda _p: {"mcpServers": {}}
    )
    monkeypatch.setattr(
        mcp_tools,
        "extract_stdio_server_commands",
        lambda _cfg: [("srv", "node", ["server.js"])],
    )


def test_env_override_allows_without_flag_or_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_project_with_stdio(monkeypatch, tmp_path)
    monkeypatch.setenv(_env_vars.MCP_TRUST, "1")
    # No --trust-project-mcp flag; the env override alone must allow, before any
    # trust-store lookup or interactive prompt.
    assert _check_mcp_project_trust(trust_flag=False) is True


def test_no_env_no_flag_denies_non_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_project_with_stdio(monkeypatch, tmp_path)
    monkeypatch.delenv(_env_vars.MCP_TRUST, raising=False)
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "bog_agents_cli.mcp_trust.is_project_mcp_trusted", lambda *_a, **_k: False
    )
    assert _check_mcp_project_trust(trust_flag=False) is False


def test_deny_message_names_the_wired_variable() -> None:
    """Drift guard: the non-TTY deny message must reference the same variable
    the override actually reads, so the printed instruction is not a lie.
    """
    source = inspect.getsource(_check_mcp_project_trust)
    assert _env_vars.MCP_TRUST in source
