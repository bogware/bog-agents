"""Tests for best-of-N orchestration (#31) — control flow, ranking, judging."""

from __future__ import annotations

from dataclasses import dataclass

from bog_agents_cli.best_of_n import (
    AttemptOutcome,
    AttemptSpec,
    JudgeVerdict,
    _verdict_from_grader,
    build_specs,
    pick_winner,
    run_best_of_n,
)


def _outcome(
    label: str, *, diff: str = "diff", error: str | None = None
) -> AttemptOutcome:
    return AttemptOutcome(label=label, model="m", diff=diff, error=error)


def _runner_for(mapping: dict[str, AttemptOutcome]):
    async def _run(spec: AttemptSpec) -> AttemptOutcome:
        return mapping[spec.label]

    return _run


def _judge_for(verdicts: dict[str, JudgeVerdict]):
    async def _judge(prompt: str, diff: str) -> JudgeVerdict:
        return verdicts[diff]

    return _judge


class TestRunBestOfN:
    async def test_picks_satisfied_over_higher_unsatisfied_score(self) -> None:
        specs = [AttemptSpec("a", "m"), AttemptSpec("b", "m")]
        runner = _runner_for(
            {"a": _outcome("a", diff="A"), "b": _outcome("b", diff="B")}
        )
        judge = _judge_for(
            {
                "A": JudgeVerdict(satisfied=True, score=0.6),
                "B": JudgeVerdict(satisfied=False, score=0.99),
            }
        )
        report = await run_best_of_n("task", specs, attempt_runner=runner, judge=judge)
        assert report.winner is not None
        assert (
            report.winner.outcome.label == "a"
        )  # satisfied beats higher unsatisfied score

    async def test_higher_score_wins_among_satisfied(self) -> None:
        specs = [AttemptSpec("a", "m"), AttemptSpec("b", "m")]
        runner = _runner_for(
            {"a": _outcome("a", diff="A"), "b": _outcome("b", diff="B")}
        )
        judge = _judge_for(
            {
                "A": JudgeVerdict(satisfied=True, score=0.7),
                "B": JudgeVerdict(satisfied=True, score=0.9),
            }
        )
        report = await run_best_of_n("task", specs, attempt_runner=runner, judge=judge)
        assert report.winner.outcome.label == "b"

    async def test_no_change_and_errors_never_win(self) -> None:
        specs = [AttemptSpec("a", "m"), AttemptSpec("b", "m"), AttemptSpec("c", "m")]
        runner = _runner_for(
            {
                "a": _outcome("a", diff=""),  # produced nothing
                "b": _outcome("b", error="boom"),  # failed
                "c": _outcome("c", diff="C"),  # only real change
            }
        )
        judge = _judge_for({"C": JudgeVerdict(satisfied=False, score=0.1)})
        report = await run_best_of_n("task", specs, attempt_runner=runner, judge=judge)
        assert (
            report.winner.outcome.label == "c"
        )  # even unsatisfied, it's the only change

    async def test_all_failed_yields_no_winner(self) -> None:
        specs = [AttemptSpec("a", "m")]
        runner = _runner_for({"a": _outcome("a", diff="", error="nope")})
        report = await run_best_of_n(
            "task", specs, attempt_runner=runner, judge=_judge_for({})
        )
        assert report.winner is None

    async def test_runner_exception_is_captured_not_raised(self) -> None:
        async def _boom(spec: AttemptSpec) -> AttemptOutcome:
            msg = "runner exploded"
            raise RuntimeError(msg)

        report = await run_best_of_n(
            "t", [AttemptSpec("a", "m")], attempt_runner=_boom, judge=_judge_for({})
        )
        assert report.attempts[0].outcome.error == "runner exploded"
        assert report.winner is None

    async def test_judge_exception_leaves_attempt_unjudged(self) -> None:
        async def _boom_judge(prompt: str, diff: str) -> JudgeVerdict:
            raise ValueError("judge down")

        runner = _runner_for({"a": _outcome("a", diff="A")})
        report = await run_best_of_n(
            "t", [AttemptSpec("a", "m")], attempt_runner=runner, judge=_boom_judge
        )
        assert report.attempts[0].verdict is None
        assert report.winner.outcome.label == "a"  # unjudged change still applicable


class TestBuildSpecs:
    def test_empty_models_repeats_default(self) -> None:
        specs = build_specs([], default_model="anthropic:x", n=3)
        assert len(specs) == 3
        assert all(s.model == "anthropic:x" for s in specs)
        assert len({s.label for s in specs}) == 3  # labels unique

    def test_lineup_cycles(self) -> None:
        specs = build_specs(["a", "b"], default_model="d", n=3)
        assert [s.model for s in specs] == ["a", "b", "a"]

    def test_n_clamped_to_one(self) -> None:
        assert len(build_specs([], default_model="d", n=0)) == 1


class TestVerdictMapping:
    def test_maps_grader_criteria_to_score(self) -> None:
        @dataclass
        class _FakeGrader:
            result: str
            summary: str
            criteria: list

        resp = _FakeGrader(
            result="needs_revision",
            summary="close",
            criteria=[
                {"passed": True},
                {"passed": False},
                {"passed": True},
                {"passed": False},
            ],
        )
        verdict = _verdict_from_grader(resp)
        assert verdict.satisfied is False
        assert verdict.score == 0.5  # 2/4 passed
        assert verdict.summary == "close"

    def test_satisfied_without_criteria_scores_one(self) -> None:
        @dataclass
        class _FakeGrader:
            result: str
            summary: str
            criteria: list

        verdict = _verdict_from_grader(
            _FakeGrader(result="satisfied", summary="", criteria=[])
        )
        assert verdict.satisfied is True
        assert verdict.score == 1.0


def test_pick_winner_alias_matches_report_winner() -> None:
    from bog_agents_cli.best_of_n import BestOfNReport, ScoredAttempt

    report = BestOfNReport(
        prompt="t",
        attempts=[
            ScoredAttempt(
                outcome=_outcome("a", diff="A"),
                verdict=JudgeVerdict(satisfied=True, score=0.8),
            )
        ],
    )
    assert pick_winner(report) is report.winner
    assert "winner" in report.format_summary()
