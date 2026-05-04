"""Unit tests for the /qa harness (ac, plan, executor, artifact)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bog_agents_cli.qa import (
    AcceptanceCriterion,
    QAPlan,
    QAStep,
    StepKind,
    StepVerdict,
    emit_artifact,
    execute_plan,
    find_plan,
    list_plans,
    load_acceptance_criteria,
    load_plan,
    parse_ac_from_json,
    parse_ac_from_text,
    save_plan,
)
from bog_agents_cli.qa.executor import (
    StepResult,
    _aggregate_ac_outcomes,
    _overall_verdict,
)
from bog_agents_cli.vars import VarBundle, VarSpec
from bog_agents_cli.vault import SessionVault

# ---------------------------------------------------------------------------
# AC parsing
# ---------------------------------------------------------------------------


class TestParseAcFromText:
    def test_bullets(self):
        out = parse_ac_from_text("- First AC\n- Second AC\n- Third")
        assert [ac.text for ac in out] == ["First AC", "Second AC", "Third"]
        assert [ac.id for ac in out] == ["AC1", "AC2", "AC3"]

    def test_numbered(self):
        out = parse_ac_from_text("1. one\n2) two")
        assert [ac.text for ac in out] == ["one", "two"]

    def test_paragraph_blocks(self):
        out = parse_ac_from_text("First paragraph here.\n\nSecond paragraph.\n\nThird.")
        assert len(out) == 3

    def test_gherkin_grouped_under_parent(self):
        text = "- The user can log in\n  Given a valid user\n  When they submit credentials\n  Then they see the dashboard"
        out = parse_ac_from_text(text)
        # Whole gherkin block stays under one AC.
        assert len(out) == 1
        assert "Given" in out[0].text
        assert "Then" in out[0].text

    def test_empty_returns_empty(self):
        assert parse_ac_from_text("") == []
        assert parse_ac_from_text("   \n  \n") == []

    def test_source_label(self):
        out = parse_ac_from_text("- one", source="file:foo.md")
        assert out[0].source == "file:foo.md"


class TestParseAcFromJson:
    def test_list_of_strings(self):
        out = parse_ac_from_json('["one", "two"]')
        assert [ac.text for ac in out] == ["one", "two"]

    def test_list_of_objects(self):
        out = parse_ac_from_json('[{"id":"AC9","text":"x","priority":"should"}]')
        assert out[0].id == "AC9"
        assert out[0].priority == "should"

    def test_envelope(self):
        out = parse_ac_from_json('{"acceptance_criteria":["a","b"]}')
        assert len(out) == 2

    def test_envelope_other_keys(self):
        out = parse_ac_from_json('{"criteria":["a"]}')
        assert len(out) == 1

    def test_invalid_json(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            parse_ac_from_json("{not json")

    def test_envelope_without_known_key(self):
        with pytest.raises(ValueError, match="acceptance_criteria"):
            parse_ac_from_json('{"foo": []}')

    def test_top_level_string_rejected(self):
        with pytest.raises(ValueError, match="list of criteria"):
            parse_ac_from_json('"not a list"')


class TestLoadAcceptanceCriteria:
    def test_inline_text(self):
        out = load_acceptance_criteria(inline_text="- one\n- two")
        assert len(out) == 2

    def test_from_file_md(self, tmp_path: Path):
        p = tmp_path / "ac.md"
        p.write_text("- a\n- b")
        out = load_acceptance_criteria(from_file=p)
        assert len(out) == 2

    def test_from_file_json(self, tmp_path: Path):
        p = tmp_path / "ac.json"
        p.write_text('["a", "b"]')
        out = load_acceptance_criteria(from_file=p)
        assert len(out) == 2

    def test_from_json_string(self):
        out = load_acceptance_criteria(from_json='["a"]')
        assert len(out) == 1

    def test_from_json_file_path(self, tmp_path: Path):
        p = tmp_path / "ac.json"
        p.write_text('["a", "b"]')
        out = load_acceptance_criteria(from_json=str(p))
        assert len(out) == 2

    def test_no_source_returns_empty(self):
        assert load_acceptance_criteria() == []

    def test_multiple_sources_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="exactly one"):
            load_acceptance_criteria(inline_text="a", from_json='["b"]')

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ValueError, match="not a file"):
            load_acceptance_criteria(from_file=tmp_path / "nope.md")


# ---------------------------------------------------------------------------
# Plan IO
# ---------------------------------------------------------------------------


class TestPlanIO:
    def _plan(self) -> QAPlan:
        return QAPlan(
            plan_id="qa-test",
            name="Test plan",
            product="checkout",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="works")],
            vars_spec={"base_url": {"type": "string", "default": "http://x"}},
            steps=[
                QAStep(
                    id="s1",
                    kind=StepKind.SHELL,
                    ac=["AC1"],
                    run="echo hi",
                    verdict=StepVerdict(exit_code=0, contains=["hi"]),
                ),
            ],
        )

    def test_save_and_load(self, tmp_path: Path):
        plan = self._plan()
        path = save_plan(tmp_path, plan)
        assert path.suffix == ".yaml"
        loaded = load_plan(path)
        assert loaded.plan_id == "qa-test"
        assert len(loaded.steps) == 1
        assert loaded.steps[0].kind is StepKind.SHELL

    def test_save_includes_header(self, tmp_path: Path):
        save_plan(tmp_path, self._plan())
        text = (tmp_path / ".bog-agents" / "qa-plans" / "qa-test.yaml").read_text(
            encoding="utf-8"
        )
        assert text.startswith("#")

    def test_round_trip_through_dict(self):
        plan = self._plan()
        loaded = QAPlan.from_dict(plan.to_dict())
        assert loaded.plan_id == plan.plan_id
        assert loaded.steps[0].verdict.exit_code == 0
        assert loaded.steps[0].verdict.contains == ["hi"]

    def test_list_plans_sorted_newest_first(self, tmp_path: Path):
        for sid, ts in [("a", 1), ("b", 3), ("c", 2)]:
            p = QAPlan(plan_id=sid, created_at=ts)
            save_plan(tmp_path, p)
        ids = [p.plan_id for p in list_plans(tmp_path)]
        assert ids == ["b", "c", "a"]

    def test_find_plan_exact(self, tmp_path: Path):
        save_plan(tmp_path, QAPlan(plan_id="qa-abc"))
        assert find_plan(tmp_path, "qa-abc") is not None

    def test_find_plan_substring(self, tmp_path: Path):
        save_plan(tmp_path, QAPlan(plan_id="qa-abc"))
        assert find_plan(tmp_path, "abc") is not None

    def test_find_plan_missing(self, tmp_path: Path):
        assert find_plan(tmp_path, "nope") is None

    def test_unknown_step_kind_raises(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            yaml.safe_dump({"plan_id": "x", "steps": [{"id": "s1", "kind": "weird"}]})
        )
        with pytest.raises(ValueError, match="unknown step kind"):
            load_plan(path)


# ---------------------------------------------------------------------------
# Verdict rules
# ---------------------------------------------------------------------------


class TestStepVerdict:
    def test_default_passes_with_zero_exit(self):
        v = StepVerdict()
        assert v.is_empty()

    def test_exit_code_match(self):
        ok, _ = StepVerdict(exit_code=0).evaluate(exit_code=0)
        assert ok
        bad, reason = StepVerdict(exit_code=0).evaluate(exit_code=1)
        assert not bad
        assert "exit_code" in reason

    def test_contains_required(self):
        ok, _ = StepVerdict(contains=["yes"]).evaluate(body="hello yes")
        assert ok
        bad, _ = StepVerdict(contains=["yes"]).evaluate(body="hello")
        assert not bad

    def test_not_contains(self):
        bad, reason = StepVerdict(not_contains=["error"]).evaluate(
            body="all good error here"
        )
        assert not bad
        assert "forbidden" in reason

    def test_regex_match(self):
        ok, _ = StepVerdict(regex=[r"\d+ items"]).evaluate(body="42 items")
        assert ok
        bad, _ = StepVerdict(regex=[r"\d+ items"]).evaluate(body="no items")
        assert not bad

    def test_invalid_regex_reports_clearly(self):
        bad, reason = StepVerdict(regex=["[unclosed"]).evaluate(body="any")
        assert not bad
        assert "invalid regex" in reason

    def test_status_list(self):
        ok, _ = StepVerdict(status=[200, 201, 204]).evaluate(status=201)
        assert ok
        bad, _ = StepVerdict(status=[200]).evaluate(status=404)
        assert not bad

    def test_json_path_present(self):
        ok, _ = StepVerdict(json_path="data.id").evaluate(
            body="", json_data={"data": {"id": 1}}
        )
        assert ok

    def test_json_path_missing(self):
        bad, _ = StepVerdict(json_path="data.id").evaluate(
            body="", json_data={"data": {}}
        )
        assert not bad


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class TestExecutor:
    def _bundle(self) -> VarBundle:
        return VarBundle(vault=SessionVault())

    async def test_shell_step_success(self):
        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="echo")],
            steps=[
                QAStep(
                    id="s1",
                    kind=StepKind.SHELL,
                    ac=["AC1"],
                    run="python -c \"print('hello')\"",
                    verdict=StepVerdict(exit_code=0, contains=["hello"]),
                )
            ],
        )
        result = await execute_plan(plan, self._bundle())
        assert result.overall_verdict == "pass"
        assert result.step_results[0].passed
        assert "hello" in result.step_results[0].output

    async def test_shell_step_failure(self):
        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[
                QAStep(
                    id="s1",
                    kind=StepKind.SHELL,
                    ac=["AC1"],
                    run='python -c "import sys; sys.exit(2)"',
                    verdict=StepVerdict(exit_code=0),
                )
            ],
        )
        result = await execute_plan(plan, self._bundle())
        assert result.overall_verdict == "fail"
        assert not result.step_results[0].passed

    async def test_shell_step_timeout(self):
        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[
                QAStep(
                    id="s1",
                    kind=StepKind.SHELL,
                    ac=["AC1"],
                    run='python -c "import time; time.sleep(60)"',
                    timeout_s=1,
                )
            ],
        )
        result = await execute_plan(plan, self._bundle())
        assert not result.step_results[0].passed
        assert "timed out" in result.step_results[0].reason

    async def test_var_substitution_in_shell(self):
        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            vars_spec={"phrase": {"type": "string", "default": "world"}},
            steps=[
                QAStep(
                    id="s1",
                    kind=StepKind.SHELL,
                    ac=["AC1"],
                    run="python -c \"print('hi ${phrase}')\"",
                    verdict=StepVerdict(contains=["hi world"]),
                )
            ],
        )
        b = VarBundle.from_dict(plan.vars_spec, vault=SessionVault())
        b.set("phrase", "world")
        result = await execute_plan(plan, b)
        assert result.overall_verdict == "pass"

    async def test_agent_step_skipped_when_no_runner(self):
        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[QAStep(id="s1", kind=StepKind.AGENT, ac=["AC1"], prompt="do it")],
        )
        result = await execute_plan(plan, self._bundle())
        assert not result.step_results[0].passed
        assert "skipped" in result.step_results[0].reason

    async def test_agent_step_via_callback(self):
        async def fake_agent(step, prompt):
            return StepResult(
                step_id=step.id,
                kind=step.kind.value,
                started_at=0.0,
                duration_s=0.0,
                passed=True,
                reason="agent OK",
                output=f"echoed: {prompt}",
            )

        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[
                QAStep(id="s1", kind=StepKind.AGENT, ac=["AC1"], prompt="test ${x}")
            ],
            vars_spec={"x": {"type": "string", "default": "y"}},
        )
        b = VarBundle.from_dict(plan.vars_spec, vault=SessionVault())
        result = await execute_plan(plan, b, run_agent_step=fake_agent)
        assert result.step_results[0].passed
        assert "echoed: test y" in result.step_results[0].output

    async def test_abort_on_fail_stops_subsequent_steps(self):
        plan = QAPlan(
            plan_id="p1",
            acceptance_criteria=[
                AcceptanceCriterion(id="AC1", text="x"),
                AcceptanceCriterion(id="AC2", text="y"),
            ],
            steps=[
                QAStep(
                    id="s1",
                    kind=StepKind.SHELL,
                    ac=["AC1"],
                    run='python -c "import sys; sys.exit(1)"',
                    on_fail="abort",
                ),
                QAStep(
                    id="s2",
                    kind=StepKind.SHELL,
                    ac=["AC2"],
                    run="python -c \"print('never')\"",
                ),
            ],
        )
        result = await execute_plan(plan, self._bundle())
        assert result.aborted
        assert len(result.step_results) == 1
        assert result.overall_verdict == "fail"


class TestACOutcomes:
    def test_ac_with_no_steps_is_inconclusive(self):
        plan = QAPlan(
            plan_id="p",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[],
        )
        outcomes = _aggregate_ac_outcomes(plan, [])
        assert outcomes[0].verdict == "inconclusive"

    def test_all_steps_pass_means_ac_passes(self):
        plan = QAPlan(
            plan_id="p",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[
                QAStep(id="s1", kind=StepKind.SHELL, ac=["AC1"]),
                QAStep(id="s2", kind=StepKind.SHELL, ac=["AC1"]),
            ],
        )
        results = [
            StepResult(
                step_id="s1", kind="shell", started_at=0, duration_s=0, passed=True
            ),
            StepResult(
                step_id="s2", kind="shell", started_at=0, duration_s=0, passed=True
            ),
        ]
        outcomes = _aggregate_ac_outcomes(plan, results)
        assert outcomes[0].verdict == "pass"

    def test_any_step_failure_fails_ac(self):
        plan = QAPlan(
            plan_id="p",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="x")],
            steps=[
                QAStep(id="s1", kind=StepKind.SHELL, ac=["AC1"]),
                QAStep(id="s2", kind=StepKind.SHELL, ac=["AC1"]),
            ],
        )
        results = [
            StepResult(
                step_id="s1", kind="shell", started_at=0, duration_s=0, passed=True
            ),
            StepResult(
                step_id="s2",
                kind="shell",
                started_at=0,
                duration_s=0,
                passed=False,
                reason="x",
            ),
        ]
        outcomes = _aggregate_ac_outcomes(plan, results)
        assert outcomes[0].verdict == "fail"
        assert outcomes[0].failed_step_ids == ["s2"]

    def test_overall_verdict_fail_dominates(self):
        v = _overall_verdict(
            [
                _outcome("AC1", "pass"),
                _outcome("AC2", "fail"),
                _outcome("AC3", "inconclusive"),
            ],
            aborted=False,
        )
        assert v == "fail"

    def test_overall_inconclusive_when_no_pass_no_fail(self):
        v = _overall_verdict([_outcome("AC1", "inconclusive")], aborted=False)
        assert v == "inconclusive"

    def test_overall_aborted_is_fail(self):
        v = _overall_verdict([_outcome("AC1", "pass")], aborted=True)
        assert v == "fail"


def _outcome(ac_id: str, verdict: str):
    from bog_agents_cli.qa.executor import ACOutcome

    return ACOutcome(ac_id=ac_id, text="t", verdict=verdict)


# ---------------------------------------------------------------------------
# Artifact emitters
# ---------------------------------------------------------------------------


class TestArtifacts:
    def _result(self, verdict: str = "pass"):
        from bog_agents_cli.qa.executor import ACOutcome, ExecutionResult

        plan = QAPlan(
            plan_id="p1",
            name="Test",
            acceptance_criteria=[AcceptanceCriterion(id="AC1", text="works")],
        )
        result = ExecutionResult(
            plan_id="p1",
            run_id="run-1",
            started_at=0.0,
            duration_s=1.5,
            overall_verdict=verdict,
            step_results=[
                StepResult(
                    step_id="s1",
                    kind="shell",
                    started_at=0.0,
                    duration_s=0.5,
                    passed=verdict == "pass",
                    reason="ok",
                    output="hello",
                    exit_code=0,
                )
            ],
            ac_outcomes=[ACOutcome(ac_id="AC1", text="works", verdict=verdict)],
        )
        return plan, result

    def test_markdown_renders(self, tmp_path: Path):
        plan, result = self._result("pass")
        text, path = emit_artifact(plan, result, fmt="markdown", out_dir=tmp_path)
        assert "QA Report" in text
        assert "AC1" in text
        assert path is not None
        assert path.exists()
        assert path.suffix == ".md"

    def test_markdown_marks_failure(self):
        plan, result = self._result("fail")
        text, _ = emit_artifact(plan, result, fmt="markdown")
        assert "FAIL" in text

    def test_json_format(self, tmp_path: Path):
        plan, result = self._result("pass")
        text, path = emit_artifact(plan, result, fmt="json", out_dir=tmp_path)
        data = json.loads(text)
        assert data["plan"]["id"] == "p1"
        assert data["result"]["overall_verdict"] == "pass"
        assert path is not None
        assert path.exists()
        assert path.suffix == ".json"

    def test_stdout_format(self):
        plan, result = self._result("pass")
        text, path = emit_artifact(plan, result, fmt="stdout")
        assert path is None
        assert "PASS" in text

    def test_jira_comment_format(self):
        plan, result = self._result("fail")
        text, path = emit_artifact(plan, result, fmt="jira-comment")
        assert path is None
        assert "FAIL" in text
        assert "| AC | Verdict |" in text

    def test_unknown_format_rejected(self):
        plan, result = self._result()
        with pytest.raises(ValueError, match="unknown artifact format"):
            emit_artifact(plan, result, fmt="rtf")
