"""Tests for #25 — CTX-3 pricing normalization + per-agent ledger + runaway caps."""

from __future__ import annotations

from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.middleware.cost_tracker import (
    CostTracker,
    _normalize_model_for_pricing,
    price_for_model,
)


class TestPricingNormalization:
    def test_plain_base_id(self) -> None:
        assert price_for_model("claude-opus-4-6") == (15.0, 75.0)

    def test_provider_prefix_resolves(self) -> None:
        # CTX-3: exact-match used to miss this and bill the (5,15) default.
        assert price_for_model("anthropic:claude-opus-4-6") == (15.0, 75.0)

    def test_bedrock_id_resolves(self) -> None:
        assert price_for_model("us.anthropic.claude-opus-4-6-v1:0") == (15.0, 75.0)

    def test_dated_suffix_resolves(self) -> None:
        assert price_for_model("claude-sonnet-4-6-20250107") == (3.0, 15.0)

    def test_openrouter_route_resolves(self) -> None:
        assert price_for_model("openrouter/anthropic/claude-sonnet-4-6") == (3.0, 15.0)

    def test_family_fallback(self) -> None:
        # An unknown specific opus id falls back to the opus family, not default.
        assert price_for_model("claude-opus-4-99") == (15.0, 75.0)

    def test_unknown_returns_none(self) -> None:
        assert price_for_model("totally-made-up-model") is None
        assert price_for_model("") is None

    def test_normalizer_strips_layers(self) -> None:
        assert _normalize_model_for_pricing("us.anthropic.claude-opus-4-6-v1:0") == "claude-opus-4-6"
        assert _normalize_model_for_pricing("anthropic:claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_tracker_prices_prefixed_model_correctly(self) -> None:
        # The end-to-end CTX-3 fix: a full spec no longer bills at the default.
        opus = CostTracker(model_name="anthropic:claude-opus-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
        assert opus.estimated_cost_usd == 90.0  # 15 + 75, not the 5+15 default
        default = CostTracker(model_name="unpriced-xyz", input_tokens=1_000_000, output_tokens=0)
        assert default.estimated_cost_usd == 5.0  # documented default for unpriced


class TestCostLedgerAttribution:
    def test_tracker_for_creates_and_reuses(self) -> None:
        ledger = CostLedger()
        t1 = ledger.tracker_for("worker-1", model_name="claude-sonnet-4-6")
        t2 = ledger.tracker_for("worker-1")
        assert t1 is t2
        assert ledger.tracker_for("worker-2") is not t1

    def test_totals_aggregate_across_agents(self) -> None:
        ledger = CostLedger()
        ledger.tracker_for("a", model_name="claude-opus-4-6").record_usage(input_tokens=1_000_000)
        ledger.tracker_for("b", model_name="claude-haiku-4-5").record_usage(input_tokens=1_000_000)
        # opus input 15 + haiku input 0.80
        assert round(ledger.total_cost_usd, 2) == 15.80
        assert ledger.total_tokens == 2_000_000

    def test_format_tree_ranks_by_cost(self) -> None:
        ledger = CostLedger()
        ledger.tracker_for("cheap", model_name="claude-haiku-4-5").record_usage(input_tokens=1_000_000)
        ledger.tracker_for("pricey", model_name="claude-opus-4-6").record_usage(input_tokens=1_000_000)
        tree = ledger.format_tree()
        assert tree.index("pricey") < tree.index("cheap")  # most expensive first
        assert "Total:" in tree


class TestRunawayCaps:
    def test_subagent_cap_allows_then_denies(self) -> None:
        ledger = CostLedger(caps=RunawayCaps(max_subagents=2))
        assert ledger.register_subagent_spawn().allowed is True
        assert ledger.register_subagent_spawn().allowed is True
        denied = ledger.register_subagent_spawn()
        assert denied.allowed is False
        assert "cap reached" in denied.reason
        assert ledger.subagent_spawns == 2  # a denied spawn is not counted

    def test_uncapped_always_allows(self) -> None:
        ledger = CostLedger()  # no caps
        for _ in range(50):
            assert ledger.register_subagent_spawn().allowed is True

    def test_web_search_cap(self) -> None:
        ledger = CostLedger(caps=RunawayCaps(max_web_searches=1))
        assert ledger.register_web_search().allowed is True
        assert ledger.register_web_search().allowed is False

    def test_cost_cap_denies_when_exceeded(self) -> None:
        ledger = CostLedger(caps=RunawayCaps(max_cost_usd=10.0))
        assert ledger.check_cost().allowed is True
        ledger.tracker_for("a", model_name="claude-opus-4-6").record_usage(input_tokens=1_000_000)  # $15
        decision = ledger.check_cost()
        assert decision.allowed is False
        assert "cost cap" in decision.reason
