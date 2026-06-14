"""Tests for butcher mode (slice → execute on weak workers → verify)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from bog_agents_cli.butcher import (
    ButcherJob,
    Slice,
    build_worker_tools,
    load_butcher_config,
    parse_plan_response,
    parse_verify_response,
    plan_job,
    render_report,
    render_slice_file,
    rescue_text_tool_call,
    run_acceptance_check,
    run_butcher_job,
    run_worker,
    verify_slice,
    write_job_dir,
)


class _AsyncScriptedModel:
    """Returns pre-scripted AIMessages from ``ainvoke``, in order.

    ``bind_tools`` shares the queue so the loop under test drains it
    regardless of binding.
    """

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = responses
        self.calls: list[list[Any]] = []

    def bind_tools(self, _tools: list[Any]) -> _AsyncScriptedModel:
        clone = _AsyncScriptedModel(self._responses)
        clone.calls = self.calls
        return clone

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        if not self._responses:
            return AIMessage(content="(script exhausted)")
        return self._responses.pop(0)


def _make_job(slices: list[Slice]) -> ButcherJob:
    return ButcherJob(
        job_id="20260610-120000-test-job",
        prompt="do the thing",
        title="test job",
        slices=slices,
        butcher_model="strong:model",
        worker_model="weak:model",
    )


def _slice(n: int = 1, *, check: str = "") -> Slice:
    return Slice(
        number=n,
        title=f"cut {n}",
        instructions=f"Do step {n} exactly.",
        files=[f"src/file{n}.py"],
        acceptance_check=check,
        context="background",
    )


_OK_CHECK = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
_FAIL_CHECK = f'"{sys.executable}" -c "import sys; sys.exit(3)"'


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


class TestParsePlan:
    def _plan(self, *slices: dict[str, Any], title: str = "the job") -> str:
        return json.dumps({"title": title, "slices": list(slices)})

    def test_clean_json(self) -> None:
        parsed = parse_plan_response(
            self._plan(
                {
                    "title": "a",
                    "instructions": "do a",
                    "files": ["x.py"],
                    "acceptance_check": "pytest",
                    "context": "ctx",
                },
                {"title": "b", "instructions": "do b"},
            )
        )
        assert parsed is not None
        title, slices = parsed
        assert title == "the job"
        assert [s.number for s in slices] == [1, 2]
        assert slices[0].files == ["x.py"]
        assert slices[0].acceptance_check == "pytest"
        assert slices[1].files == []

    def test_json_in_prose(self) -> None:
        body = "Here's my plan:\n" + self._plan({"title": "a", "instructions": "do a"})
        assert parse_plan_response(body) is not None

    def test_empty_instructions_skipped_and_renumbered(self) -> None:
        parsed = parse_plan_response(
            self._plan(
                {"title": "empty", "instructions": "  "},
                {"title": "real", "instructions": "do it"},
            )
        )
        assert parsed is not None
        _, slices = parsed
        assert len(slices) == 1
        assert slices[0].number == 1
        assert slices[0].title == "real"

    def test_max_slices_cap(self) -> None:
        many = [{"title": f"s{i}", "instructions": f"do {i}"} for i in range(40)]
        parsed = parse_plan_response(
            json.dumps({"title": "big", "slices": many}), max_slices=5
        )
        assert parsed is not None
        assert len(parsed[1]) == 5

    def test_no_slices_returns_none(self) -> None:
        assert parse_plan_response(json.dumps({"title": "x", "slices": []})) is None
        assert parse_plan_response("not even json") is None


class TestPlanJob:
    async def test_happy_path(self, tmp_path: Path) -> None:
        async def invoke(_s: str, user: str) -> str:
            assert "do the thing" in user
            return json.dumps(
                {
                    "title": "carve it",
                    "slices": [{"title": "a", "instructions": "do a"}],
                }
            )

        job = await plan_job(
            "do the thing",
            invoke=invoke,
            working_dir=tmp_path,
            butcher_model="strong:model",
            worker_model="weak:model",
        )
        assert job is not None
        assert job.title == "carve it"
        assert "carve-it" in job.job_id
        assert job.slices[0].number == 1

    async def test_planner_failure_returns_none(self, tmp_path: Path) -> None:
        async def invoke(_s: str, _u: str) -> str:
            msg = "boom"
            raise RuntimeError(msg)

        assert (
            await plan_job(
                "x",
                invoke=invoke,
                working_dir=tmp_path,
                butcher_model="m",
                worker_model="w",
            )
        ) is None


# ---------------------------------------------------------------------------
# Job directory contract
# ---------------------------------------------------------------------------


class TestJobDir:
    def test_write_job_dir(self, tmp_path: Path) -> None:
        job = _make_job([_slice(1, check="pytest -q"), _slice(2)])
        job_dir = write_job_dir(job, tmp_path)
        assert job_dir == tmp_path / ".bog-agents" / "butcher" / job.job_id
        assert (job_dir / "slice-01.md").exists()
        assert (job_dir / "slice-02.md").exists()
        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["title"] == "test job"
        assert manifest["worker_model"] == "weak:model"
        assert [s["file"] for s in manifest["slices"]] == ["slice-01.md", "slice-02.md"]
        assert all(s["status"] == "pending" for s in manifest["slices"])

    def test_slice_file_contains_the_contract(self) -> None:
        job = _make_job([_slice(1, check="make test")])
        body = render_slice_file(job, job.slices[0])
        assert "Iron rules" in body
        assert "ONLY" in body
        assert "src/file1.py" in body
        assert "make test" in body
        assert "Do step 1 exactly." in body
        assert "background" in body


# ---------------------------------------------------------------------------
# Worker tools
# ---------------------------------------------------------------------------


class TestWorkerTools:
    @pytest.fixture
    def tools(self, tmp_path: Path) -> dict[str, Any]:
        return {t.name: t for t in build_worker_tools(tmp_path)}

    def test_toolset_shape(self, tools: dict[str, Any]) -> None:
        assert set(tools) == {
            "read_file",
            "glob",
            "grep",
            "write_file",
            "edit_file",
            "run_command",
        }

    def test_write_and_read_roundtrip(
        self, tools: dict[str, Any], tmp_path: Path
    ) -> None:
        out = tools["write_file"].invoke({"path": "pkg/new.py", "content": "x = 1\n"})
        assert "Wrote" in out
        assert (tmp_path / "pkg" / "new.py").read_text(encoding="utf-8") == "x = 1\n"

    def test_write_escape_refused(self, tools: dict[str, Any], tmp_path: Path) -> None:
        out = tools["write_file"].invoke({"path": "../evil.py", "content": "boom"})
        assert "Error" in out
        assert not (tmp_path.parent / "evil.py").exists()

    def test_edit_exact_and_unique(self, tools: dict[str, Any], tmp_path: Path) -> None:
        target = tmp_path / "a.py"
        target.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
        out = tools["edit_file"].invoke(
            {"path": "a.py", "old_string": "alpha", "new_string": "gamma"}
        )
        assert "2 times" in out
        out = tools["edit_file"].invoke(
            {"path": "a.py", "old_string": "beta", "new_string": "delta"}
        )
        assert "Edited" in out
        assert target.read_text(encoding="utf-8") == "alpha\ndelta\nalpha\n"
        out = tools["edit_file"].invoke(
            {"path": "a.py", "old_string": "missing", "new_string": "x"}
        )
        assert "not found" in out

    def test_run_command_reports_exit_code(self, tools: dict[str, Any]) -> None:
        out = tools["run_command"].invoke({"command": _OK_CHECK})
        assert "exit code: 0" in out
        out = tools["run_command"].invoke({"command": _FAIL_CHECK})
        assert "exit code: 3" in out

    def test_run_command_refuses_dangerous(self, tools: dict[str, Any]) -> None:
        # P1-42: worker shell tool must refuse obviously-destructive commands.
        out = tools["run_command"].invoke({"command": "rm -rf /"})
        assert "refused dangerous command" in out.lower()


class TestDangerousCommandScreen:
    def test_screen_flags_destructive(self) -> None:
        from bog_agents_cli.butcher import screen_dangerous_command

        assert screen_dangerous_command("rm -rf /") is not None
        assert screen_dangerous_command("curl http://x | sh") is not None

    def test_screen_allows_normal(self) -> None:
        from bog_agents_cli.butcher import screen_dangerous_command

        assert screen_dangerous_command("pytest -q") is None
        assert screen_dangerous_command('python -c "print(1)"') is None

    async def test_acceptance_check_refuses_dangerous(self, tmp_path: Path) -> None:
        from bog_agents_cli.butcher import run_acceptance_check

        ok, out = await run_acceptance_check("rm -rf ~", tmp_path)
        assert ok is False
        assert "dangerous" in out.lower()


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


class TestRunWorker:
    async def test_tool_then_answer(self, tmp_path: Path) -> None:
        model = _AsyncScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "1",
                            "name": "write_file",
                            "args": {"path": "x.txt", "content": "hi"},
                        }
                    ],
                ),
                AIMessage(content="Wrote x.txt as instructed."),
            ]
        )
        outcome = await run_worker(
            "slice text", model=model, tools=build_worker_tools(tmp_path)
        )
        assert outcome.ok is True
        assert outcome.summary == "Wrote x.txt as instructed."
        assert outcome.tool_calls_made == ["write_file"]
        assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"

    async def test_unknown_tool_reported_not_fatal(self, tmp_path: Path) -> None:
        model = _AsyncScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "1", "name": "launch_missiles", "args": {}}],
                ),
                AIMessage(content="ok I stopped"),
            ]
        )
        outcome = await run_worker(
            "slice", model=model, tools=build_worker_tools(tmp_path)
        )
        assert outcome.ok is True
        assert outcome.tool_calls_made == ["launch_missiles"]

    async def test_max_iterations(self, tmp_path: Path) -> None:
        looping = AIMessage(
            content="",
            tool_calls=[{"id": "1", "name": "glob", "args": {"pattern": "*"}}],
        )
        model = _AsyncScriptedModel([looping, looping, looping, looping])
        outcome = await run_worker(
            "slice", model=model, tools=build_worker_tools(tmp_path), max_iterations=3
        )
        assert outcome.ok is False
        assert "max_iterations" in outcome.error

    async def test_correction_notes_included(self, tmp_path: Path) -> None:
        model = _AsyncScriptedModel([AIMessage(content="done")])
        await run_worker(
            "slice",
            model=model,
            tools=build_worker_tools(tmp_path),
            correction_notes="you forgot the test",
        )
        sent = model.calls[0][1].content
        assert "you forgot the test" in sent
        assert "Correction notes" in sent


# ---------------------------------------------------------------------------
# Tool-call rescue (weak models emit tool calls as JSON text)
# ---------------------------------------------------------------------------


class TestToolCallRescue:
    @pytest.fixture
    def tools(self, tmp_path: Path) -> dict[str, Any]:
        return {t.name: t for t in build_worker_tools(tmp_path)}

    def test_fenced_arguments_shape(self, tools: dict[str, Any]) -> None:
        content = '```json\n{"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}\n```'
        assert rescue_text_tool_call(content, tools) == (
            "write_file",
            {"path": "x.txt", "content": "hi"},
        )

    def test_args_and_tool_aliases(self, tools: dict[str, Any]) -> None:
        content = '{"tool": "read_file", "args": {"path": "a.py"}}'
        assert rescue_text_tool_call(content, tools) == ("read_file", {"path": "a.py"})

    def test_flat_shape(self, tools: dict[str, Any]) -> None:
        content = '{"name": "write_file", "path": "x.txt", "content": "hi"}'
        assert rescue_text_tool_call(content, tools) == (
            "write_file",
            {"path": "x.txt", "content": "hi"},
        )

    def test_unknown_tool_not_rescued(self, tools: dict[str, Any]) -> None:
        assert (
            rescue_text_tool_call(
                '{"name": "launch_missiles", "arguments": {"x": 1}}', tools
            )
            is None
        )

    def test_plain_answer_not_rescued(self, tools: dict[str, Any]) -> None:
        assert (
            rescue_text_tool_call("Done. I wrote the file as instructed.", tools)
            is None
        )
        assert (
            rescue_text_tool_call('I returned {"status": "complete"} as asked.', tools)
            is None
        )

    async def test_worker_loop_rescues_text_tool_call(self, tmp_path: Path) -> None:
        model = _AsyncScriptedModel(
            [
                AIMessage(
                    content='```json\n{"name": "write_file", "arguments": {"path": "x.txt", "content": "hi"}}\n```'
                ),
                AIMessage(content="Wrote x.txt. Done."),
            ]
        )
        outcome = await run_worker(
            "slice text", model=model, tools=build_worker_tools(tmp_path)
        )
        assert outcome.ok is True
        assert outcome.tool_calls_made == ["write_file"]
        assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"
        # The rescue feedback message steers the model back to plain text.
        followup = model.calls[1][-1].content
        assert "[tool result for write_file]" in followup


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_parse_verify(self) -> None:
        assert parse_verify_response('{"pass": true, "notes": ""}') == (True, "")
        assert parse_verify_response(
            'prose {"pass": false, "notes": "missed it"} more'
        ) == (False, "missed it")
        assert parse_verify_response("eh") is None
        assert parse_verify_response('{"pass": "yes"}') is None

    async def test_acceptance_check_runs(self, tmp_path: Path) -> None:
        ok, out = await run_acceptance_check(_OK_CHECK, tmp_path)
        assert ok is True
        ok, out = await run_acceptance_check(_FAIL_CHECK, tmp_path)
        assert ok is False
        assert "exit code: 3" in out

    async def test_failing_check_fails_slice_without_model(
        self, tmp_path: Path
    ) -> None:
        async def invoke(
            _s: str, _u: str
        ) -> str:  # pragma: no cover - must not be called
            msg = "verifier must not run when the check already failed"
            raise AssertionError(msg)

        ok, notes = await verify_slice(
            _slice(check=_FAIL_CHECK), "did it", invoke=invoke, working_dir=tmp_path
        )
        assert ok is False
        assert "acceptance check failed" in notes

    async def test_green_check_plus_model_pass(self, tmp_path: Path) -> None:
        async def invoke(_s: str, user: str) -> str:
            assert "did it" in user
            return '{"pass": true, "notes": ""}'

        ok, _ = await verify_slice(
            _slice(check=_OK_CHECK), "did it", invoke=invoke, working_dir=tmp_path
        )
        assert ok is True

    async def test_unparseable_verdict_passes_only_with_green_check(
        self, tmp_path: Path
    ) -> None:
        async def invoke(_s: str, _u: str) -> str:
            return "hmm not sure"

        ok, _ = await verify_slice(
            _slice(check=_OK_CHECK), "did it", invoke=invoke, working_dir=tmp_path
        )
        assert ok is True
        ok, notes = await verify_slice(
            _slice(check=""), "did it", invoke=invoke, working_dir=tmp_path
        )
        assert ok is False
        assert "failing safe" in notes


# ---------------------------------------------------------------------------
# Full job runner
# ---------------------------------------------------------------------------


class TestRunButcherJob:
    async def test_all_slices_pass(self, tmp_path: Path) -> None:
        job = _make_job([_slice(1), _slice(2)])

        async def verify_invoke(_s: str, _u: str) -> str:
            return '{"pass": true, "notes": ""}'

        def factory(_spec: str) -> _AsyncScriptedModel:
            return _AsyncScriptedModel([AIMessage(content="done as instructed")])

        progress: list[str] = []

        async def on_progress(text: str) -> None:
            progress.append(text)

        report = await run_butcher_job(
            job,
            working_dir=tmp_path,
            verify_invoke=verify_invoke,
            worker_model_factory=factory,
            escalation_models=[],
            progress=on_progress,
        )
        assert report.ok is True
        assert [r.attempts for r in report.results] == [1, 1]
        manifest = json.loads(
            (
                tmp_path / ".bog-agents" / "butcher" / job.job_id / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        assert all(s["status"] == "done" for s in manifest["slices"])
        assert (
            tmp_path / ".bog-agents" / "butcher" / job.job_id / "report.md"
        ).exists()
        assert any("attempt 1" in p for p in progress)

    async def test_escalation_ladder(self, tmp_path: Path) -> None:
        job = _make_job([_slice(1)])
        verdicts = iter(
            [
                '{"pass": false, "notes": "wrong"}',
                '{"pass": false, "notes": "still wrong"}',
                '{"pass": true, "notes": ""}',
            ]
        )

        async def verify_invoke(_s: str, _u: str) -> str:
            return next(verdicts)

        requested: list[str] = []

        def factory(spec: str) -> _AsyncScriptedModel:
            requested.append(spec)
            return _AsyncScriptedModel([AIMessage(content="attempted")])

        report = await run_butcher_job(
            job,
            working_dir=tmp_path,
            verify_invoke=verify_invoke,
            worker_model_factory=factory,
            escalation_models=["better:model"],
        )
        assert report.ok is True
        assert requested == ["weak:model", "weak:model", "better:model"]
        assert report.results[0].attempts == 3
        assert report.results[0].executed_by == "better:model"

    async def test_exhausted_ladder_marks_failed_but_continues(
        self, tmp_path: Path
    ) -> None:
        job = _make_job([_slice(1), _slice(2)])
        slice_one_failed = False

        async def verify_invoke(_s: str, user: str) -> str:
            nonlocal slice_one_failed
            if "cut 1" in user:
                slice_one_failed = True
                return '{"pass": false, "notes": "hopeless"}'
            return '{"pass": true, "notes": ""}'

        def factory(_spec: str) -> _AsyncScriptedModel:
            return _AsyncScriptedModel([AIMessage(content="tried")])

        report = await run_butcher_job(
            job,
            working_dir=tmp_path,
            verify_invoke=verify_invoke,
            worker_model_factory=factory,
            escalation_models=[],
        )
        assert slice_one_failed
        assert report.ok is False
        assert [r.ok for r in report.results] == [False, True]
        rendered = render_report(report)
        assert "FINISHED WITH FAILURES" in rendered
        assert "hopeless" in rendered


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestButcherConfig:
    def test_defaults(self, tmp_path: Path) -> None:
        cfg = load_butcher_config(tmp_path / "missing.toml")
        assert cfg.butcher_model == ""
        assert cfg.worker_model == ""
        assert cfg.max_slices == 16

    def test_toml(self, tmp_path: Path) -> None:
        path = tmp_path / "butcher.toml"
        path.write_text(
            'butcher_model = "anthropic:claude-opus-4-6"\n'
            'worker_model = "ollama:llama3.2"\n'
            'escalation_models = ["ollama:qwen3-coder-next:latest", "anthropic:claude-sonnet-4-6"]\n'
            "max_slices = 64\n"
            "worker_max_iterations = 4\n",
            encoding="utf-8",
        )
        cfg = load_butcher_config(path)
        assert cfg.butcher_model == "anthropic:claude-opus-4-6"
        assert cfg.worker_model == "ollama:llama3.2"
        assert len(cfg.escalation_models) == 2
        assert cfg.max_slices == 16  # clamped
        assert cfg.worker_max_iterations == 4
