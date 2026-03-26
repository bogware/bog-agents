"""Tests for server graph MCP loading behavior."""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bog_agents_cli._server_config import ServerConfig
from bog_agents_cli._server_constants import ENV_PREFIX


def _import_fresh_server_graph() -> ModuleType:
    """Import `bog_agents_cli.server_graph` from a clean module state."""
    sys.modules.pop("bog_agents_cli.server_graph", None)
    return importlib.import_module("bog_agents_cli.server_graph")


def _module_with_attrs(name: str, **attrs: object) -> ModuleType:
    """Create a module stub with dynamically assigned attributes."""
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class TestServerGraph:
    """Tests for server-mode graph bootstrap."""

    def test_auto_discovery_loads_mcp_without_explicit_config(self) -> None:
        """Server mode should auto-discover MCP configs when no path is passed."""
        graph_obj = object()
        model_obj = object()
        http_tool = object()
        fetch_tool = object()
        mcp_tool = object()
        mcp_server_info = [SimpleNamespace(name="docs")]
        create_cli_agent = MagicMock(return_value=(graph_obj, object()))
        agent_module = _module_with_attrs(
            "bog_agents_cli.agent",
            DEFAULT_AGENT_NAME="agent",
            create_cli_agent=create_cli_agent,
        )

        model_result = SimpleNamespace(
            model=model_obj,
            apply_to_settings=MagicMock(),
        )
        config_module = _module_with_attrs(
            "bog_agents_cli.config",
            create_model=MagicMock(return_value=model_result),
            create_model_with_fallback=MagicMock(return_value=model_result),
            settings=SimpleNamespace(
                has_tavily=False,
                reload_from_environment=MagicMock(),
            ),
        )

        tools_module = _module_with_attrs(
            "bog_agents_cli.tools",
            http_request=http_tool,
            fetch_url=fetch_tool,
            web_search=object(),
        )

        resolve_mcp_tools = AsyncMock(return_value=([mcp_tool], None, mcp_server_info))
        mcp_module = _module_with_attrs(
            "bog_agents_cli.mcp_tools",
            resolve_and_load_mcp_tools=resolve_mcp_tools,
        )

        # Build env from ServerConfig to exercise the same serialization
        # path the real CLI uses.
        config = ServerConfig(no_mcp=False)
        env_overrides = {}
        for suffix, value in config.to_env().items():
            if value is not None:
                env_overrides[f"{ENV_PREFIX}{suffix}"] = value

        with (
            patch.dict(os.environ, env_overrides, clear=False),
            patch.dict(
                sys.modules,
                {
                    "bog_agents_cli.agent": agent_module,
                    "bog_agents_cli.config": config_module,
                    "bog_agents_cli.tools": tools_module,
                    "bog_agents_cli.mcp_tools": mcp_module,
                },
            ),
            patch(
                "bog_agents_cli.project_utils.get_server_project_context",
                return_value=None,
            ),
        ):
            for suffix in (
                "MCP_CONFIG_PATH",
                "TRUST_PROJECT_MCP",
                "CWD",
                "PROJECT_ROOT",
            ):
                os.environ.pop(f"{ENV_PREFIX}{suffix}", None)

            module = _import_fresh_server_graph()

        resolve_mcp_tools.assert_awaited_once_with(
            explicit_config_path=None,
            no_mcp=False,
            trust_project_mcp=None,
            project_context=None,
        )
        create_cli_agent.assert_called_once_with(
            model=model_obj,
            assistant_id="agent",
            tools=[http_tool, fetch_tool, mcp_tool],
            sandbox=None,
            sandbox_type=None,
            system_prompt=None,
            interactive=True,
            auto_approve=False,
            enable_memory=True,
            enable_skills=True,
            enable_shell=True,
            mcp_server_info=mcp_server_info,
            cwd=None,
            project_context=None,
        )
        assert module.graph is graph_obj
