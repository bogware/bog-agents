"""Baseline tests for bog_agents_cli.mcp_config_manager.

This module manages ``~/.bog-agents/.mcp.json``. Each test patches the
module-level ``_USER_MCP_CONFIG`` to a per-test temp path so the user's
real file is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bog_agents_cli import mcp_config_manager as mcm


@pytest.fixture
def isolated_config(tmp_path: Path):
    cfg = tmp_path / ".mcp.json"
    with patch.object(mcm, "_USER_MCP_CONFIG", cfg):
        yield cfg


class TestLoadAndSave:
    def test_load_returns_empty_when_missing(self, isolated_config: Path):
        assert mcm.load_user_mcp_config() == {"mcpServers": {}}

    def test_load_recovers_from_non_dict_top_level(self, isolated_config: Path):
        isolated_config.parent.mkdir(parents=True, exist_ok=True)
        isolated_config.write_text("[]", encoding="utf-8")
        assert mcm.load_user_mcp_config() == {"mcpServers": {}}

    def test_load_recovers_from_invalid_json(self, isolated_config: Path):
        isolated_config.parent.mkdir(parents=True, exist_ok=True)
        isolated_config.write_text("{not json", encoding="utf-8")
        assert mcm.load_user_mcp_config() == {"mcpServers": {}}

    def test_load_adds_mcp_servers_key_when_missing(self, isolated_config: Path):
        isolated_config.parent.mkdir(parents=True, exist_ok=True)
        isolated_config.write_text(json.dumps({"otherField": 1}), encoding="utf-8")
        loaded = mcm.load_user_mcp_config()
        assert loaded["mcpServers"] == {}
        assert loaded["otherField"] == 1

    def test_save_round_trip(self, isolated_config: Path):
        data = {"mcpServers": {"foo": {"command": "echo", "args": ["hi"]}}}
        assert mcm.save_user_mcp_config(data) is True
        assert isolated_config.exists()
        assert mcm.load_user_mcp_config() == data

    def test_save_uses_atomic_write_no_tmp_leaks(self, isolated_config: Path):
        mcm.save_user_mcp_config({"mcpServers": {"x": {}}})
        # No .tmp files should remain in the parent directory.
        leftovers = [p for p in isolated_config.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_save_is_owner_only(self, isolated_config: Path):
        # The config can embed resolved vault secrets; it must not be
        # group/other readable. (REVIEW.md v2 P1-22.)
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX permission bits not honoured on Windows")
        mcm.save_user_mcp_config(
            {
                "mcpServers": {
                    "s": {
                        "type": "sse",
                        "url": "http://x",
                        "headers": {"Authorization": "Bearer tok"},
                    }
                }
            }
        )
        mode = isolated_config.stat().st_mode
        assert mode & 0o077 == 0, (
            f".mcp.json with secrets is group/other accessible: {oct(mode)}"
        )


class TestServerCRUD:
    def test_add_server_fresh(self, isolated_config: Path):
        assert mcm.add_server("alpha", {"command": "x"}) is True
        assert mcm.server_exists("alpha")
        assert mcm.get_server("alpha") == {"command": "x"}

    def test_add_server_collision_without_overwrite_returns_false(
        self, isolated_config: Path
    ):
        mcm.add_server("alpha", {"command": "x"})
        assert mcm.add_server("alpha", {"command": "y"}) is False
        assert mcm.get_server("alpha") == {"command": "x"}

    def test_add_server_collision_with_overwrite_replaces(self, isolated_config: Path):
        mcm.add_server("alpha", {"command": "x"})
        assert mcm.add_server("alpha", {"command": "y"}, overwrite=True) is True
        assert mcm.get_server("alpha") == {"command": "y"}

    def test_remove_server_when_present(self, isolated_config: Path):
        mcm.add_server("alpha", {"command": "x"})
        assert mcm.remove_server("alpha") is True
        assert not mcm.server_exists("alpha")

    def test_remove_server_when_missing(self, isolated_config: Path):
        assert mcm.remove_server("nonexistent") is False

    def test_list_servers(self, isolated_config: Path):
        mcm.add_server("a", {"command": "x"})
        mcm.add_server("b", {"command": "y"})
        listed = mcm.list_servers()
        assert set(listed.keys()) == {"a", "b"}


class TestPathHelper:
    def test_get_user_mcp_config_path_returns_module_constant(self):
        # Without patching, this should match the module-level constant.
        assert mcm.get_user_mcp_config_path() == mcm._USER_MCP_CONFIG
