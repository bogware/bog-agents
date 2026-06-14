"""Tests for the `bog_agents.evals` SDK primitive (ROADMAP #9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents.evals import (
    Case,
    Contains,
    Dataset,
    ExactMatch,
    Regex,
    run_evals,
)


class TestDataset:
    def test_from_list_dicts(self) -> None:
        ds = Dataset.from_list([{"input": "a", "expected": "b"}], name="d")
        assert len(ds) == 1
        assert ds.cases[0].input == "a"
        assert ds.name == "d"

    def test_from_list_cases(self) -> None:
        ds = Dataset.from_list([Case(input="x", expected="y")])
        assert ds.cases[0].expected == "y"

    def test_from_json(self, tmp_path: Path) -> None:
        p = tmp_path / "cases.json"
        p.write_text(json.dumps([{"input": "1+1", "expected": "2"}]), encoding="utf-8")
        ds = Dataset.from_json(p)
        assert len(ds) == 1
        assert ds.name == "cases"


class TestRuleScorers:
    def test_exact_match(self) -> None:
        s = ExactMatch()
        assert s.score(Case(input="", expected="Paris"), "paris").passed  # case-insensitive
        assert not s.score(Case(input="", expected="Paris"), "London").passed

    def test_contains(self) -> None:
        s = Contains()
        assert s.score(Case(input="", expected="cat"), "the cat sat").passed
        assert not s.score(Case(input="", expected="dog"), "the cat sat").passed

    def test_regex(self) -> None:
        s = Regex(pattern=r"\d{3}-\d{4}")
        assert s.score(Case(input=""), "call 555-1234").passed
        assert not s.score(Case(input=""), "no number").passed


class TestRunEvals:
    async def test_pass_rate_and_report(self) -> None:
        ds = Dataset.from_list(
            [
                {"input": "say paris", "expected": "Paris"},
                {"input": "say london", "expected": "London"},
            ],
            name="caps",
        )

        # Task always answers "Paris" -> 1/2 pass.
        def task(_inp: str) -> str:
            return "Paris"

        report = await run_evals(task, ds, [Contains()])
        assert report.total == 2
        assert report.passed == 1
        assert report.pass_rate == 0.5
        assert "caps" in report.summary()

    async def test_async_task_supported(self) -> None:
        ds = Dataset.from_list([{"input": "x", "expected": "ok"}])

        async def task(_inp: str) -> str:
            return "ok"

        report = await run_evals(task, ds, [ExactMatch()])
        assert report.pass_rate == 1.0

    async def test_task_crash_is_failed_case_not_runner_crash(self) -> None:
        ds = Dataset.from_list([{"input": "x", "expected": "ok"}])

        def task(_inp: str) -> str:
            raise RuntimeError("boom")

        report = await run_evals(task, ds, [ExactMatch()])
        assert report.passed == 0
        assert "boom" in report.results[0].error

    async def test_assert_pass_rate_raises_below_threshold(self) -> None:
        ds = Dataset.from_list([{"input": "x", "expected": "yes"}])
        report = await run_evals(lambda _i: "no", ds, [ExactMatch()])
        with pytest.raises(AssertionError):
            report.assert_pass_rate(0.9)

    async def test_scorer_averages(self) -> None:
        ds = Dataset.from_list(
            [{"input": "a", "expected": "a"}, {"input": "b", "expected": "b"}]
        )
        report = await run_evals(lambda _i: "a", ds, [ExactMatch()])
        avgs = report.scorer_averages()
        assert avgs["exact_match"] == 0.5
