"""ROADMAP #73: agent-authored workflows — schema, authoring, the persisted runner, resume, gates, budget."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bog_agents.cost_ledger import RunawayCaps

from bog_agents_cli import workflow as wf

YAML = """
name: ship-fix
description: Research, fix, verify and summarise a ticket.
args: [ticket]
budget_usd: 1.0
phases:
  - name: research
    kind: context
    workers: 2
    tasks:
      - id: map
        title: Map the code for {ticket}
      - id: repro
        title: Reproduce {ticket}
  - name: implement
    kind: work
    tasks:
      - id: change
        title: Fix {ticket}
        prompt: "Use this: {context}"
        depends_on: [map, repro]
  - name: check
    kind: verify
    tasks:
      - id: tests
        title: Run the tests
  - name: summary
    kind: synthesize
    tasks:
      - Summarise the change
"""


def test_parse_render_round_trip_and_validation(tmp_path: Path) -> None:
    workflow = wf.parse_workflow(YAML)
    assert (
        workflow.name == "ship-fix"
        and workflow.args == ("ticket",)
        and workflow.budget_usd == 1.0
    )
    assert [p.kind for p in workflow.phases] == [
        "context",
        "work",
        "verify",
        "synthesize",
    ]
    assert workflow.phases[2].gate and not workflow.phases[1].gate
    assert workflow.phases[3].tasks[0].id == "summary-1" and workflow.task_count == 5
    again = wf.parse_workflow(wf.render_workflow_yaml(workflow))
    assert (
        again == wf.Workflow(**{**again.__dict__, "source": ""})
        and again.phases == workflow.phases
    )
    assert workflow.usage() == "/ship-fix <ticket>"

    for bad, message in [
        ("name: Bad Name\nphases: [{kind: work, tasks: [x]}]", "slug"),
        ("name: ok\nphases: []", "non-empty"),
        ("name: ok\nphases: [{kind: dance, tasks: [x]}]", "kind"),
        (
            "name: ok\nphases: [{kind: work, tasks: [{id: a, title: x}, {id: a, title: y}]}]",
            "duplicate",
        ),
        (
            "name: ok\nphases: [{kind: work, tasks: [{title: x, depends_on: nope}]}]",
            "unknown task",
        ),
        ("name: ok\nphases: [{kind: work, workers: 0, tasks: [x]}]", "workers"),
        ("name: [", "parse"),
    ]:
        with pytest.raises(ValueError, match=message):
            wf.parse_workflow(bad)

    path = wf.save_workflow(tmp_path, workflow)
    assert path == tmp_path / ".bog-agents" / "workflows" / "ship-fix.yaml"
    (path.parent / "broken.yaml").write_text("name: [", encoding="utf-8")
    assert list(wf.discover_workflows(tmp_path)) == ["ship-fix"]
    assert "ship-fix" in wf.describe_workflows(
        list(wf.discover_workflows(tmp_path).values())
    )
    shown = wf.describe_workflow(workflow)
    assert "verify" in shown and "[gate]" in shown and "after map, repro" in shown


def test_bind_args() -> None:
    workflow = wf.parse_workflow(YAML)
    assert wf.bind_args(workflow, "ABC-1 extra words") == {
        "ticket": "ABC-1 extra words",
        "args": "ABC-1 extra words",
    }
    with pytest.raises(ValueError, match="missing ticket"):
        wf.bind_args(workflow, "")


def test_author_workflow_retries_then_saves(tmp_path: Path) -> None:
    replies = iter(["```yaml\nname: nope\nphases: []\n```", f"```yaml\n{YAML}\n```"])
    prompts: list[str] = []

    def _invoke(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    path, workflow = wf.author_workflow(
        "ship a fix", invoke=_invoke, project_root=tmp_path, name="ship-fix"
    )
    assert path.is_file() and workflow.name == "ship-fix" and len(prompts) == 2
    assert "rejected" in prompts[1] and "non-empty" in prompts[1]
    with pytest.raises(ValueError, match="could not author"):
        wf.author_workflow(
            "x", invoke=lambda _p: "name: [", project_root=tmp_path, retries=0
        )


def _runner(*, fail_verdict: bool = False, cost: float = 0.1):
    calls: list[str] = []

    async def run(
        task: wf.WorkflowTask, prompt: str, phase: wf.PhaseRecord
    ) -> wf.TaskOutcome:
        calls.append(f"{phase.name}/{task.id}")
        if phase.kind == "verify":
            verdict = (
                "VERDICT: FAIL\ntests red"
                if fail_verdict
                else "all green\nVERDICT: PASS"
            )
            return wf.TaskOutcome(
                success=True,
                output=verdict,
                cost_usd=cost,
                input_tokens=10,
                output_tokens=5,
            )
        if task.id in {"map", "repro", "change"}:
            assert "ABC-1" in prompt
        if task.id == "change":
            assert (
                "### research / map" in prompt
                and "Context from earlier phases" not in prompt
            )  # {context} used explicitly
        if task.id == "tests":
            assert "VERDICT: PASS" in prompt
        return wf.TaskOutcome(
            success=True,
            output=f"did {task.id}",
            cost_usd=cost,
            input_tokens=10,
            output_tokens=5,
        )

    return run, calls


def test_run_workflow_end_to_end_with_meters(tmp_path: Path) -> None:
    workflow = wf.parse_workflow(YAML)
    runner, calls = _runner()
    saved: list[str] = []
    run = asyncio.run(
        wf.run_workflow(
            workflow,
            runner=runner,
            args=wf.bind_args(workflow, "ABC-1"),
            persist=lambda r: saved.append(r.status),
            on_event=lambda _m: None,
        )
    )
    assert run.status == "done" and run.result == "did summary-1"
    assert calls[:2] in (
        ["research/map", "research/repro"],
        ["research/repro", "research/map"],
    ) and calls[2:] == ["implement/change", "check/tests", "summary/summary-1"]
    assert (
        run.spent_usd == pytest.approx(0.5)
        and run.tokens == (50, 25)
        and run.activations == 5
    )
    assert (
        all(p.done for p in run.phases) and run.phases[2].tasks["tests"].passed is True
    )
    assert "running" in saved and saved[-1] == "done"
    path = run.save(wf.run_path(tmp_path, run))
    loaded = wf.WorkflowRun.load(path)
    assert (
        loaded.to_dict() == run.to_dict()
        and wf.latest_run(tmp_path, "ship-fix") is not None
    )
    assert "spend $0.5000" in loaded.format_summary()


def test_gate_failure_stops_the_run() -> None:
    workflow = wf.parse_workflow(YAML)
    runner, calls = _runner(fail_verdict=True)
    run = asyncio.run(
        wf.run_workflow(workflow, runner=runner, args={"ticket": "ABC-1"})
    )
    assert run.status == "failed" and "check: gate failed: tests" in run.stop_reason
    assert (
        run.phases[2].status == "failed"
        and run.phases[3].status == "pending"
        and "summary/summary-1" not in calls
    )


def test_budget_pauses_and_resume_skips_done_phases() -> None:
    workflow = wf.parse_workflow(YAML)  # budget 1.0
    runner, calls = _runner(
        cost=0.4
    )  # research (2 tasks) = 0.8, implement = 1.2 → pause after implement
    run = asyncio.run(
        wf.run_workflow(
            workflow, runner=runner, args={"ticket": "ABC-1"}, caps=RunawayCaps()
        )
    )
    assert run.status == "paused" and "budget" in run.stop_reason
    assert [p.status for p in run.phases] == ["done", "done", "pending", "pending"]
    first_calls = list(calls)

    cheap, calls2 = _runner(cost=0.0)
    stuck = asyncio.run(
        wf.run_workflow(
            workflow,
            runner=cheap,
            args={"ticket": "ABC-1"},
            run=run,
            caps=RunawayCaps(),
        )
    )
    assert stuck.status == "paused" and "higher budget" in stuck.stop_reason
    assert not calls2
    resumed = asyncio.run(
        wf.run_workflow(
            workflow,
            runner=cheap,
            args={"ticket": "ABC-1"},
            run=run,
            caps=RunawayCaps(),
            budget_usd=5.0,
        )
    )
    assert (
        resumed.status == "done"
        and calls2 == ["check/tests", "summary/summary-1"]
        and len(first_calls) == 3
    )


def test_spawn_cap_pauses_a_phase() -> None:
    workflow = wf.parse_workflow(YAML)
    runner, _calls = _runner()
    run = asyncio.run(
        wf.run_workflow(
            workflow,
            runner=runner,
            args={"ticket": "ABC-1"},
            caps=RunawayCaps(max_subagents=1),
        )
    )
    assert run.status == "paused" and "spawn cap" in run.stop_reason
    assert (
        run.phases[0].tasks["map"].status == "done"
        and run.phases[0].tasks["repro"].status == "pending"
    )


def test_failed_task_fails_the_phase() -> None:
    workflow = wf.parse_workflow(YAML)

    async def boom(
        task: wf.WorkflowTask, _prompt: str, _phase: wf.PhaseRecord
    ) -> wf.TaskOutcome:
        if task.id == "repro":
            msg = "no repro"
            raise RuntimeError(msg)
        return wf.TaskOutcome(success=True, output="ok")

    run = asyncio.run(wf.run_workflow(workflow, runner=boom, args={"ticket": "ABC-1"}))
    assert (
        run.status == "failed"
        and "repro" in run.stop_reason
        and run.phases[1].status == "pending"
    )
