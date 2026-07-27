"""Operator-pinned RBAC / air-gap: the model cannot lift its own restrictions.

MW-SAFE-2 / MW-SAFE-1 (v4): both middleware previously exposed their own
policy-mutation tools to the model (set_active_role/define_role,
set_data_policy/clear_air_gap), so they only bounded a cooperative model. When an
operator pins a role/policy, those mutators are withheld and enforcement is
deny-by-default.
"""

from __future__ import annotations

from typing import Any

import pytest

from bog_agents import create_agent
from bog_agents.feature_config import FeatureConfig
from bog_agents.middleware.air_gapped import AirGappedMiddleware, DataPolicy
from bog_agents.middleware.rbac import RBACMiddleware, Role


def _tool_names(mw: Any) -> set[str]:
    return {t.name for t in mw.tools}


class TestRBACPinning:
    def test_pinned_removes_mutator_tools(self) -> None:
        mw = RBACMiddleware(
            active_role="analyst",
            roles=[Role(name="analyst", allowed_tools=["read_*"])],
        )
        assert _tool_names(mw) == {"check_permission", "list_roles"}
        assert mw.store.strict is True
        assert mw.store.active_role == "analyst"

    def test_unpinned_keeps_all_tools(self) -> None:
        mw = RBACMiddleware()
        assert _tool_names(mw) == {
            "define_role",
            "set_active_role",
            "check_permission",
            "list_roles",
        }
        assert mw.store.strict is False

    def test_pinned_undefined_role_denies_all(self) -> None:
        mw = RBACMiddleware(active_role="ghost")  # no such role defined
        assert mw.store.strict is True
        assert mw.store.is_allowed("read_file") is False
        assert mw.store.is_allowed("anything_at_all") is False

    def test_pinned_role_enforces_allowlist(self) -> None:
        mw = RBACMiddleware(
            active_role="reader",
            roles=[Role(name="reader", allowed_tools=["read_*"], denied_tools=["read_secret"])],
        )
        assert mw.store.is_allowed("read_file") is True
        assert mw.store.is_allowed("read_secret") is False
        assert mw.store.is_allowed("write_file") is False


class TestAirGapPinning:
    def test_pinned_removes_mutator_tools(self) -> None:
        mw = AirGappedMiddleware(policy=DataPolicy(allow_external=False))
        assert _tool_names(mw) == {"register_local_model", "check_data_flow", "air_gap_status"}
        assert mw._operator_pinned is True

    def test_unpinned_keeps_all_tools(self) -> None:
        mw = AirGappedMiddleware()
        names = _tool_names(mw)
        assert "set_data_policy" in names
        assert "clear_air_gap" in names
        assert len(mw.tools) == 5

    def test_pinned_policy_governs_egress(self) -> None:
        policy = DataPolicy(allow_external=True, allowed_domains=["ok.internal"])
        mw = AirGappedMiddleware(policy=policy)
        assert mw.store.policy is policy
        allowed, _ = mw.store.check_allowed("ok.internal")
        assert allowed is True
        blocked, _ = mw.store.check_allowed("evil.example")
        assert blocked is False


def _capture_middleware(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> list[Any]:
    """Build an agent and capture the middleware instances graph.py assembled."""
    from bog_agents import graph as graph_module

    captured: list[Any] = []
    original = graph_module._validate_middleware_ordering

    def _spy(middleware_list: list[Any]) -> None:
        captured.extend(middleware_list)
        return original(middleware_list)

    monkeypatch.setattr(graph_module, "_validate_middleware_ordering", _spy)
    create_agent(model="claude-sonnet-4-20250514", **kwargs)
    return captured


class TestFlagPathIsPinned:
    def test_enable_air_gapped_pins_fail_closed_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mws = _capture_middleware(monkeypatch, config=FeatureConfig(enable_air_gapped=True))
        ag = next(m for m in mws if isinstance(m, AirGappedMiddleware))
        assert ag._operator_pinned is True
        assert "set_data_policy" not in {t.name for t in ag.tools}
        assert ag.store.policy.allow_external is False  # fail-closed default

    def test_enable_rbac_with_pinned_role_removes_mutators(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = FeatureConfig(
            enable_rbac=True,
            rbac_active_role="reader",
            rbac_roles=[Role(name="reader", allowed_tools=["read_*"])],
        )
        mws = _capture_middleware(monkeypatch, config=cfg)
        rbac = next(m for m in mws if isinstance(m, RBACMiddleware))
        assert "set_active_role" not in {t.name for t in rbac.tools}
        assert rbac.store.strict is True
