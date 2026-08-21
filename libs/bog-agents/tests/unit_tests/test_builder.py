"""Tests for the AgentBuilder fluent API."""

from __future__ import annotations

import warnings

import pytest

from bog_agents.builder import AgentBuilder
from bog_agents.feature_config import FeatureConfig
from bog_agents.middleware.parallel_agents import ParallelAgentsMiddleware


def _capture_create_agent(monkeypatch) -> dict:
    """Patch create_agent to capture the kwargs build() forwards."""
    captured: dict = {}

    def fake_create_agent(**kwargs: object) -> str:
        captured.update(kwargs)
        return "GRAPH"

    monkeypatch.setattr("bog_agents.graph.create_agent", fake_create_agent)
    return captured


def test_with_multi_agent_wires_parallel_agents_middleware(monkeypatch) -> None:
    """with_multi_agent() wires the live ParallelAgentsMiddleware.

    The in-process orchestrator was removed in V1, so the builder must no
    longer forward the dead ``enable_multi_agent`` / ``max_agent_threads``
    flags — it adds the ``parallel_tasks`` middleware instead.
    """
    captured: dict = {}

    def fake_create_agent(**kwargs: object) -> str:
        captured.update(kwargs)
        return "GRAPH"

    monkeypatch.setattr("bog_agents.graph.create_agent", fake_create_agent)

    result = AgentBuilder("anthropic:claude-sonnet-4-6").with_multi_agent().build()

    assert result == "GRAPH"
    middleware = captured.get("middleware", [])
    assert any(isinstance(m, ParallelAgentsMiddleware) for m in middleware)
    # The dead orchestrator flags must no longer be forwarded.
    assert "enable_multi_agent" not in captured
    assert "max_agent_threads" not in captured


def test_build_without_multi_agent_omits_parallel_middleware(monkeypatch) -> None:
    captured: dict = {}

    def fake_create_agent(**kwargs: object) -> str:
        captured.update(kwargs)
        return "GRAPH"

    monkeypatch.setattr("bog_agents.graph.create_agent", fake_create_agent)

    AgentBuilder("anthropic:claude-sonnet-4-6").build()

    middleware = captured.get("middleware", [])
    assert not any(isinstance(m, ParallelAgentsMiddleware) for m in middleware)


# --------------------------------------------------------------------------- #
# SDK-CORE-2 — feature flags via FeatureConfig, cost not force-enabled
# --------------------------------------------------------------------------- #


def test_build_does_not_force_enable_cost_tracking(monkeypatch) -> None:
    captured = _capture_create_agent(monkeypatch)
    AgentBuilder("anthropic:claude-sonnet-4-6").build()
    # Cost tracking must not be silently on, and never via the bare-kwarg backdoor.
    assert "enable_cost_tracking" not in captured
    cfg = captured.get("config")
    assert cfg is None or cfg.enable_cost_tracking is False


def test_build_routes_feature_flags_through_config(monkeypatch) -> None:
    captured = _capture_create_agent(monkeypatch)
    AgentBuilder("anthropic:claude-sonnet-4-6").with_cost_tracking(budget_usd=5).build()
    cfg = captured.get("config")
    assert isinstance(cfg, FeatureConfig)
    assert cfg.enable_cost_tracking is True
    assert cfg.budget_usd == 5
    # Feature flags must NOT also leak through as bare (deprecated) kwargs.
    assert "enable_cost_tracking" not in captured
    assert "budget_usd" not in captured


def test_real_build_emits_no_legacy_flag_deprecation() -> None:
    # The definitive SDK-CORE-2 check: a real build() with feature flags must not
    # trip create_agent's "feature flags as kwargs" DeprecationWarning.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        agent = AgentBuilder("anthropic:claude-sonnet-4-6").with_cost_tracking(budget_usd=1).build()
    assert agent is not None
    assert not any("feature flags as kwargs" in str(w.message) for w in caught), [str(w.message) for w in caught]


# --------------------------------------------------------------------------- #
# SDKC-4 (v5) — with_kwargs(config=...) merges with builder flags, never replaces
# --------------------------------------------------------------------------- #


def test_with_kwargs_config_merges_with_builder_flags(monkeypatch) -> None:
    """A user FeatureConfig must not silently drop explicitly requested features.

    Regression: `with_git(repo_map=True).with_kwargs(config=FeatureConfig(enable_dlp=True))`
    used to forward only the user's config, discarding enable_repo_map.
    """
    captured = _capture_create_agent(monkeypatch)
    (AgentBuilder("anthropic:claude-sonnet-4-6").with_git(repo_map=True).with_kwargs(config=FeatureConfig(enable_dlp=True)).build())
    cfg = captured.get("config")
    assert isinstance(cfg, FeatureConfig)
    # Both sources survive the merge.
    assert cfg.enable_repo_map is True  # builder flag kept
    assert cfg.enable_dlp is True  # user config field kept
    # Nothing leaks through the deprecated bare-kwarg backdoor.
    assert "enable_repo_map" not in captured


def test_with_kwargs_config_lets_builder_flags_win_on_overlap(monkeypatch) -> None:
    """Explicit `with_X()` calls take precedence for the fields they set,
    mirroring `_resolve_feature_config`'s documented kwarg-over-config layering.
    Fields the builder never set come from the user's config unchanged.
    """
    captured = _capture_create_agent(monkeypatch)
    user_cfg = FeatureConfig(enable_rbac=False, rbac_active_role="reader")
    AgentBuilder("anthropic:claude-sonnet-4-6").with_rbac().with_kwargs(config=user_cfg).build()
    cfg = captured.get("config")
    assert isinstance(cfg, FeatureConfig)
    assert cfg.enable_rbac is True  # with_rbac() wins on the overlapping field
    assert cfg.rbac_active_role == "reader"  # unsurfaced field flows through
    # The user's original config object is not mutated by the merge.
    assert user_cfg.enable_rbac is False


def test_with_kwargs_config_alone_passes_through(monkeypatch) -> None:
    """With no builder feature flags, the user's config is forwarded as-is."""
    captured = _capture_create_agent(monkeypatch)
    user_cfg = FeatureConfig(enable_dlp=True)
    AgentBuilder("anthropic:claude-sonnet-4-6").with_kwargs(config=user_cfg).build()
    assert captured.get("config") is user_cfg


# --------------------------------------------------------------------------- #
# SDK-CORE-7 — mcp / sandbox no-ops made honest
# --------------------------------------------------------------------------- #


def test_with_mcp_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="with_mcp"):
        AgentBuilder("anthropic:claude-sonnet-4-6").with_mcp("github")


def test_with_sandbox_allow_dangerous_builds_local_backend(monkeypatch) -> None:
    from bog_agents.backends.local_shell import LocalShellBackend

    captured = _capture_create_agent(monkeypatch)
    AgentBuilder("anthropic:claude-sonnet-4-6").with_sandbox(allow_dangerous=True).build()
    backend = captured.get("backend")
    assert isinstance(backend, LocalShellBackend)
    assert backend._allow_dangerous is True
