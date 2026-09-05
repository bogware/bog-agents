"""ROADMAP #51: budgets that pause (budget_reached interrupt) and pre-flight estimates."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bog_agents.cost_ledger import estimate_run_cost
from bog_agents.middleware.cost_tracker import BUDGET_REACHED, CostTrackerMiddleware, parse_budget_resume

MODEL = "claude-sonnet-4-6"  # $3 / 1M input


def _spent(mw: CostTrackerMiddleware, usd: float) -> None:
    """Record enough input tokens on the tracker to reach `usd` of spend."""
    mw.tracker.record_usage(input_tokens=int(usd / 3.0 * 1_000_000))


def _request(context: Any = None) -> Any:
    return SimpleNamespace(runtime=SimpleNamespace(context=context), messages=[], state={})


def _handler(calls: list[int]) -> Any:
    def call_next(_request: Any) -> Any:
        calls.append(1)
        return SimpleNamespace(result=[])

    return call_next


class TestParseBudgetResume:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ({"budget_usd": 5}, 5.0),
            ({"type": "raise_budget", "budget_usd": "7.5"}, 7.5),
            (12, 12.0),
            ("$3.50", 3.5),
            ("1,000", 1000.0),
            ({"budget_usd": 0}, None),
            ({"budget_usd": -1}, None),
            ("abc", None),
            (True, None),
            (None, None),
            ({"decisions": []}, None),
        ],
    )
    def test_cases(self, value: Any, expected: float | None) -> None:
        assert parse_budget_resume(value) == expected


class TestBudgetInterrupt:
    def test_pauses_until_a_raise_cap_resume(self) -> None:
        payloads: list[dict[str, Any]] = []
        resumes = iter([{"budget_usd": 0.5}, "nonsense", {"budget_usd": 10}])

        def fake_interrupt(payload: dict[str, Any]) -> Any:
            payloads.append(payload)
            return next(resumes)

        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, interrupt_fn=fake_interrupt)
        _spent(mw, 2.0)
        calls: list[int] = []
        mw.wrap_model_call(_request(), _handler(calls))
        # Two resumes did not raise the cap above the $2 spend; the third did.
        assert len(payloads) == 3
        assert payloads[0]["type"] == BUDGET_REACHED
        assert payloads[0]["budget_usd"] == 1.0
        assert payloads[0]["spent_usd"] == pytest.approx(2.0, rel=1e-3)
        assert "budget_usd" in payloads[0]["resume"]
        assert mw.tracker.budget_usd == 10.0
        assert calls == [1]

    async def test_async_path_pauses_too(self) -> None:
        seen: list[dict[str, Any]] = []

        def fake_interrupt(payload: dict[str, Any]) -> Any:
            seen.append(payload)
            return {"budget_usd": 50}

        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, interrupt_fn=fake_interrupt)
        _spent(mw, 2.0)
        calls: list[int] = []

        async def call_next(_request: Any) -> Any:
            calls.append(1)
            return SimpleNamespace(result=[])

        await mw.awrap_model_call(_request(), call_next)
        assert len(seen) == 1
        assert calls == [1]

    def test_no_pause_under_budget(self) -> None:
        def fake_interrupt(_payload: dict[str, Any]) -> Any:
            raise AssertionError("must not interrupt under budget")

        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=5.0, interrupt_fn=fake_interrupt)
        _spent(mw, 1.0)
        calls: list[int] = []
        mw.wrap_model_call(_request(), _handler(calls))
        assert calls == [1]

    def test_falls_back_to_raise_without_a_checkpointer(self) -> None:
        def no_graph(_payload: dict[str, Any]) -> Any:
            raise RuntimeError("Called get_config outside of a runnable context")

        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, interrupt_fn=no_graph)
        _spent(mw, 2.0)
        with pytest.raises(RuntimeError, match="Cost budget exceeded"):
            mw.wrap_model_call(_request(), _handler([]))

    def test_raise_mode_is_the_pre_51_behaviour(self) -> None:
        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, on_budget="raise")
        _spent(mw, 2.0)
        with pytest.raises(RuntimeError, match="Cost budget exceeded"):
            mw.wrap_model_call(_request(), _handler([]))

    def test_warn_mode_and_legacy_strict_false_continue(self) -> None:
        for mw in (
            CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, on_budget="warn"),
            CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, strict_budget=False),
        ):
            _spent(mw, 2.0)
            calls: list[int] = []
            mw.wrap_model_call(_request(), _handler(calls))
            assert calls == [1]


class TestRuntimeContextBudget:
    def test_context_raises_or_lifts_the_cap(self) -> None:
        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, on_budget="raise")
        _spent(mw, 2.0)
        calls: list[int] = []
        # /cost budget 0 → unlimited for this turn.
        mw.wrap_model_call(_request({"budget_usd": 0}), _handler(calls))
        assert mw.tracker.budget_usd is None
        assert calls == [1]
        # /cost budget 1.5 → still exceeded at $2 → raise.
        with pytest.raises(RuntimeError):
            mw.wrap_model_call(_request({"budget_usd": 1.5}), _handler(calls))
        # /cost budget 5 → continue.
        mw.wrap_model_call(_request({"budget_usd": 5}), _handler(calls))
        assert mw.tracker.budget_usd == 5.0
        assert calls == [1, 1]

    def test_absent_or_bad_context_keeps_the_cap(self) -> None:
        mw = CostTrackerMiddleware(model_name=MODEL, budget_usd=1.0, on_budget="warn")
        mw.wrap_model_call(_request(None), _handler([]))
        mw.wrap_model_call(_request({"budget_usd": None}), _handler([]))
        mw.wrap_model_call(_request({"budget_usd": "9"}), _handler([]))
        mw.wrap_model_call(_request(SimpleNamespace(budget_usd=True)), _handler([]))
        assert mw.tracker.budget_usd == 1.0
        mw.wrap_model_call(_request(SimpleNamespace(budget_usd=3)), _handler([]))
        assert mw.tracker.budget_usd == 3.0


class TestEstimateRunCost:
    def test_priced_model_gives_a_bracket(self) -> None:
        est = estimate_run_cost(3, "anthropic:claude-sonnet-4-6")
        assert est.priced is True
        assert est.agents == 3
        # 3 x (60k x 3 + 4k x 15) / 1M = 0.72 ; 3 x (250k x 3 + 20k x 15) / 1M = 3.15
        assert est.low_usd == pytest.approx(0.72)
        assert est.high_usd == pytest.approx(3.15)
        assert "$0.72-$3.15" in est.format()

    def test_unpriced_model_is_honest(self) -> None:
        est = estimate_run_cost(2, "ollama:some-local-model")
        assert est.priced is False
        assert est.high_usd == 0.0
        assert "no price on file" in est.format()

    def test_zero_agents(self) -> None:
        assert estimate_run_cost(0, "claude-sonnet-4-6").high_usd == 0.0
