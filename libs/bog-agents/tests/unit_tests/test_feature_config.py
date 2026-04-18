"""Tests for FeatureConfig-first API (Item 11).

Verifies that `create_agent(config=FeatureConfig(...))` works as the primary
path, and that the `features` kwarg (backward-compat alias) continues to work.
"""

from __future__ import annotations

import pytest

from bog_agents import FeatureConfig, create_agent

MODEL = "claude-sonnet-4-20250514"


class TestFeatureConfigParameter:
    """Tests for the `config` keyword argument on `create_agent()`."""

    def test_config_none_is_accepted(self):
        """Passing config=None should work identically to omitting it."""
        agent = create_agent(model=MODEL, config=None)
        assert agent is not None

    def test_config_default_featureconfig_is_accepted(self):
        """A default FeatureConfig (all flags off) must compile without error."""
        agent = create_agent(model=MODEL, config=FeatureConfig())
        assert agent is not None

    def test_config_enable_git_tools(self):
        """config=FeatureConfig(enable_git_tools=True) must add git tools."""
        agent = create_agent(model=MODEL, config=FeatureConfig(enable_git_tools=True))
        tool_names = set(agent.nodes["tools"].bound._tools_by_name.keys())
        # GitToolsMiddleware provides at least one git-related tool
        assert any("git" in name for name in tool_names), (
            f"Expected a git tool in {sorted(tool_names)}"
        )

    def test_config_enable_repo_map(self):
        """config=FeatureConfig(enable_repo_map=True) must compile without error."""
        agent = create_agent(model=MODEL, config=FeatureConfig(enable_repo_map=True))
        assert agent is not None

    def test_config_enable_cost_tracking(self):
        """config=FeatureConfig(enable_cost_tracking=True) must compile."""
        agent = create_agent(model=MODEL, config=FeatureConfig(enable_cost_tracking=True))
        assert agent is not None

    def test_config_enable_plan_mode(self):
        """config=FeatureConfig(enable_plan_mode=True) must compile."""
        agent = create_agent(model=MODEL, config=FeatureConfig(enable_plan_mode=True))
        assert agent is not None

    def test_features_alias_still_works(self):
        """The `features` kwarg must still be accepted for backward compat."""
        agent = create_agent(model=MODEL, features=FeatureConfig())
        assert agent is not None

    def test_config_takes_precedence_over_features(self):
        """When both `config` and `features` are given, `config` wins."""
        # config has git tools enabled; features does not
        agent = create_agent(
            model=MODEL,
            config=FeatureConfig(enable_git_tools=True),
            features=FeatureConfig(enable_git_tools=False),
        )
        tool_names = set(agent.nodes["tools"].bound._tools_by_name.keys())
        assert any("git" in name for name in tool_names), (
            "config should win over features — git tools should be present"
        )

    def test_config_is_keyword_only(self):
        """The `config` parameter must be keyword-only (cannot pass positionally)."""
        with pytest.raises(TypeError):
            create_agent(MODEL, None, FeatureConfig())  # type: ignore[call-arg]

    def test_featureconfig_exported_from_package(self):
        """FeatureConfig must be importable from the top-level `bog_agents` package."""
        import bog_agents

        assert hasattr(bog_agents, "FeatureConfig")
        assert bog_agents.FeatureConfig is FeatureConfig

    def test_config_multiple_flags(self):
        """A FeatureConfig with multiple flags enabled must compile without error."""
        cfg = FeatureConfig(
            enable_git_tools=True,
            enable_repo_map=True,
            enable_cost_tracking=True,
            enable_plan_mode=True,
        )
        agent = create_agent(model=MODEL, config=cfg)
        assert agent is not None
