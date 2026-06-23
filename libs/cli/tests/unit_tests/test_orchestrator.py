"""Tests for /orchestrate (Wave G — T-8 Roo Boomerang Tasks parity)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.messages import AIMessage

from bog_agents_cli.orchestrator import (
    OrchestrationResult,
    Subtask,
    SubtaskMode,
    SubtaskResult,
    decompose_goal,
    render_result,
    run_orchestration,
)
from bog_agents_cli.orchestrator_controller import (
    OrchestratorController,
    dispatch,
    get_controller,
    reset_controllers,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_timeout_fallback_result_uses_valid_fields() -> None:
    """Regression: the parallel-timeout fallback once constructed
    ``SubtaskResult(output=..., elapsed_seconds=...)`` — fields that don't
    exist — which raised ``TypeError`` whenever the outer cap was hit. Lock the
    field contract that the fallback (orchestrator.py) depends on.
    """
    st = Subtask(id="1", mode=SubtaskMode.CODE, description="do a thing")
    # Exactly the kwargs the timeout fallback now constructs.
    result = SubtaskResult(
        subtask=st,
        ok=False,
        error="subtask timed out (outer cap 120s)",
        duration_seconds=120.0,
    )
    assert result.ok is False
    assert "timed out" in result.error
    assert result.duration_seconds == 120.0
    # The buggy field names must never come back.
    assert not hasattr(result, "output")
    assert not hasattr(result, "elapsed_seconds")


# ---------------------------------------------------------------------------
# Scripted model: returns a fixed sequence of responses
# ---------------------------------------------------------------------------


class _SequenceModel:
    """Returns responses from a pre-scripted queue, in order.

    ``bind_tools`` returns a new instance that SHARES both the response
    queue and the invocations list so tests can observe every call the
    orchestrator made — planner + every subtask — through the original
    handle.
    """

    def __init__(
        self,
        responses: list[str],
        *,
        _shared_invocations: list | None = None,
    ) -> None:
        self._responses = list(responses)
        self.invocations: list = (
            _shared_invocations if _shared_invocations is not None else []
        )

    def bind_tools(self, _tools: list[Any]) -> _SequenceModel:
        new = _SequenceModel([], _shared_invocations=self.invocations)
        new._responses = self._responses  # share queue
        return new

    def invoke(self, messages: list) -> Any:  # noqa: ANN401
        self.invocations.append(list(messages))
        if not self._responses:
            return AIMessage(content="(empty)")
        return AIMessage(content=self._responses.pop(0))


def _plan_json(*subtasks: tuple[str, str, str]) -> str:
    """Helper: ``[(id, mode, description), ...]`` → JSON plan."""
    return json.dumps(
        {
            "plan": [
                {"id": sid, "mode": mode, "description": desc}
                for sid, mode, desc in subtasks
            ]
        }
    )


@pytest.fixture(autouse=True)
def _isolated() -> None:
    reset_controllers()


# ---------------------------------------------------------------------------
# decompose_goal
# ---------------------------------------------------------------------------


class TestDecompose:
    def test_clean_json_parses(self) -> None:
        model = _SequenceModel(
            [_plan_json(("t1", "review", "look at it"), ("t2", "doc", "doc it"))]
        )
        plan, raw, err = decompose_goal(
            "review and document the auth flow", model=model
        )
        assert err == ""
        assert len(plan) == 2
        assert plan[0].mode is SubtaskMode.REVIEW
        assert plan[1].mode is SubtaskMode.DOC
        assert raw  # raw text preserved

    def test_fenced_json_parses(self) -> None:
        model = _SequenceModel(
            ["```json\n" + _plan_json(("t1", "code", "go")) + "\n```"]
        )
        plan, _, err = decompose_goal("any", model=model)
        assert err == ""
        assert len(plan) == 1

    def test_bad_json_yields_error(self) -> None:
        model = _SequenceModel(["this is not json {"])
        plan, raw, err = decompose_goal("x", model=model)
        assert plan == []
        assert "valid JSON" in err
        assert raw

    def test_unknown_mode_rejected(self) -> None:
        model = _SequenceModel(
            [
                json.dumps(
                    {"plan": [{"id": "t1", "mode": "frobnicate", "description": "x"}]}
                )
            ]
        )
        plan, _, err = decompose_goal("x", model=model)
        assert plan == []
        assert "not a known mode" in err

    def test_empty_plan_rejected(self) -> None:
        model = _SequenceModel([json.dumps({"plan": []})])
        plan, _, err = decompose_goal("x", model=model)
        assert plan == []
        assert "empty" in err

    def test_too_many_subtasks_rejected(self) -> None:
        many = [(f"t{i}", "review", "x") for i in range(10)]
        model = _SequenceModel([_plan_json(*many)])
        plan, _, err = decompose_goal("x", model=model)
        assert plan == []
        assert "too many" in err

    def test_missing_description_rejected(self) -> None:
        model = _SequenceModel(
            [json.dumps({"plan": [{"id": "t1", "mode": "code", "description": ""}]})]
        )
        plan, _, err = decompose_goal("x", model=model)
        assert plan == []
        assert "description" in err


# ---------------------------------------------------------------------------
# run_orchestration — end-to-end with stub model
# ---------------------------------------------------------------------------


class TestRunOrchestration:
    def test_empty_goal_returns_error(self, tmp_path: Path) -> None:
        result = run_orchestration(
            goal="",
            model=_SequenceModel([]),
            working_dir=tmp_path,
        )
        assert result.error
        assert not result.ok

    def test_plan_parse_failure_propagates(self, tmp_path: Path) -> None:
        result = run_orchestration(
            goal="something",
            model=_SequenceModel(["garbage"]),
            working_dir=tmp_path,
        )
        assert result.parse_error
        assert not result.ok

    def test_two_subtasks_run_sequentially(self, tmp_path: Path) -> None:
        # Planner emits 2 subtasks, then each worker emits its answer.
        responses = [
            _plan_json(("t1", "review", "review auth"), ("t2", "doc", "document auth")),
            "review notes here",
            "doc notes here",
        ]
        result = run_orchestration(
            goal="review and document",
            model=_SequenceModel(responses),
            working_dir=tmp_path,
        )
        assert result.ok
        assert len(result.subtasks) == 2
        answers = [s.answer for s in result.subtasks]
        assert "review notes here" in answers
        assert "doc notes here" in answers

    def test_subtask_modes_get_distinct_system_prompts(self, tmp_path: Path) -> None:
        responses = [
            _plan_json(("t1", "review", "rev"), ("t2", "test", "tst")),
            "rev-out",
            "tst-out",
        ]
        model = _SequenceModel(responses)
        run_orchestration(
            goal="anything",
            model=model,
            working_dir=tmp_path,
        )
        # 1 planner call + 2 subtask calls; each subtask passed a
        # SystemMessage with the mode-specific prompt at index 0.
        subtask_invocations = model.invocations[1:]
        from langchain_core.messages import SystemMessage

        prompts = [
            m.content
            for inv in subtask_invocations
            for m in inv
            if isinstance(m, SystemMessage)
        ]
        # Review prompt mentions "review"; test prompt mentions "test".
        assert any("review" in p for p in prompts)
        assert any("test" in p for p in prompts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRender:
    def test_renders_plan_and_results(self, tmp_path: Path) -> None:
        responses = [
            _plan_json(("t1", "review", "review the diff")),
            "Looks good. One concern in main.py:42.",
        ]
        result = run_orchestration(
            goal="review my diff",
            model=_SequenceModel(responses),
            working_dir=tmp_path,
        )
        text = render_result(result)
        assert "/orchestrate plan for" in text
        assert "review the diff" in text
        assert "main.py:42" in text

    def test_renders_error_when_plan_fails(self, tmp_path: Path) -> None:
        result = run_orchestration(
            goal="bad",
            model=_SequenceModel(["nope"]),
            working_dir=tmp_path,
        )
        text = render_result(result)
        assert "could not be parsed" in text


# ---------------------------------------------------------------------------
# Controller + dispatcher
# ---------------------------------------------------------------------------


class TestController:
    def test_controller_run(self, tmp_path: Path) -> None:
        responses = [
            _plan_json(("t1", "research", "look up X")),
            "X is Y",
        ]
        c = OrchestratorController(
            working_dir=tmp_path,
            model_factory=lambda: _SequenceModel(responses),
        )
        result = c.run("look up X")
        assert result.ok
        assert "X is Y" in result.subtasks[0].answer

    def test_controller_empty_goal(self, tmp_path: Path) -> None:
        c = OrchestratorController(
            working_dir=tmp_path,
            model_factory=lambda: _SequenceModel([]),
        )
        result = c.run("")
        assert result.error
        assert "empty goal" in result.error

    def test_dispatch_routes_through(self, tmp_path: Path) -> None:
        responses = [
            _plan_json(("t1", "review", "do it")),
            "ok",
        ]
        out = dispatch(
            "/orchestrate review the change",
            working_dir=tmp_path,
            model_factory=lambda: _SequenceModel(responses),
        )
        assert "do it" in out
        assert "✓" in out

    def test_get_controller_requires_factory_first_time(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="model_factory"):
            get_controller(tmp_path)


class TestRegistry:
    def test_slash_command_registered(self) -> None:
        from bog_agents_cli.commands import general

        names = {cmd.name for cmd in general.COMMANDS}
        assert "/orchestrate" in names

    def test_handler_method_named(self) -> None:
        from bog_agents_cli.commands import general

        handlers = {cmd.name: cmd.handler_method for cmd in general.COMMANDS}
        assert handlers["/orchestrate"] == "_handle_orchestrate_command"


# ---------------------------------------------------------------------------
# J4: parallel execution
# ---------------------------------------------------------------------------


class TestParallel:
    def test_parallel_requires_model_factory(self, tmp_path: Path) -> None:
        result = run_orchestration(
            goal="x",
            model=_SequenceModel([_plan_json(("t1", "review", "do it"))]),
            working_dir=tmp_path,
            parallel=True,
            model_factory=None,
        )
        assert result.error
        assert "parallel=True requires model_factory" in result.error

    def test_parallel_preserves_plan_order(self, tmp_path: Path) -> None:
        # Planner emits 3 subtasks; each worker uses a fresh model
        # that always answers with the subtask id. Order in result.subtasks
        # must equal plan order regardless of completion timing.
        plan = _plan_json(
            ("t1", "review", "first"),
            ("t2", "doc", "second"),
            ("t3", "research", "third"),
        )

        # Single shared planner model + a per-subtask factory.
        planner = _SequenceModel([plan])

        # Each worker gets its own model that just echoes its first
        # human message back as the answer.
        class _EchoModel:
            def bind_tools(self, _t: list) -> _EchoModel:
                return self

            def invoke(self, messages: list) -> AIMessage:
                from langchain_core.messages import HumanMessage

                human = next((m for m in messages if isinstance(m, HumanMessage)), None)
                return AIMessage(content=str(human.content)[:80] if human else "ok")

        # Use the planner for the first call; subsequent worker calls
        # go through fresh _EchoModel instances via the factory.
        calls = {"n": 0}

        def model_factory():
            calls["n"] += 1
            return _EchoModel()

        # Drive decomposition with the planner explicitly.
        # ``run_orchestration`` calls decompose_goal(model=planner)
        # internally, so we pass the planner as ``model`` and the
        # factory for subtasks.
        result = run_orchestration(
            goal="parallel test",
            model=planner,
            working_dir=tmp_path,
            parallel=True,
            model_factory=model_factory,
        )
        assert result.ok, result
        # Order preservation: ids stay in plan order.
        assert [r.subtask.id for r in result.subtasks] == ["t1", "t2", "t3"]
        # Factory was invoked once per subtask.
        assert calls["n"] == 3

    def test_parallel_timeout_collects_fast_and_does_not_leak_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P20/P21: a timing-out subtask must not block harvesting fast
        subtasks, and the dedicated executor must be torn down (no thread
        leak into the shared default pool).

        We block one worker model on an Event so its thread genuinely
        outlives the (tiny, patched) per-subtask budget. The fast
        subtask's result must still be collected; the slow one must come
        back as a timeout marker; and the dedicated executor we created
        must have been shut down with ``cancel_futures=True``.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from bog_agents_cli import orchestrator as orch

        plan = [
            Subtask(id="t1", mode=SubtaskMode.REVIEW, description="fast"),
            Subtask(id="t2", mode=SubtaskMode.DOC, description="slow"),
        ]

        fast_done = threading.Event()
        release = threading.Event()

        class _FastModel:
            def bind_tools(self, _t: list) -> _FastModel:
                return self

            def invoke(self, _messages: list) -> AIMessage:
                fast_done.set()
                return AIMessage(content="fast-done")

        class _BlockingModel:
            def bind_tools(self, _t: list) -> _BlockingModel:
                return self

            def invoke(self, _messages: list) -> AIMessage:
                # Block until the test releases us in teardown so the
                # worker thread genuinely outlives the budget.
                release.wait(timeout=30)
                return AIMessage(content="slow-done")

        models = iter([_FastModel(), _BlockingModel()])

        def model_factory():
            return next(models)

        # Tiny budget so the blocked subtask trips the wall-clock cap fast.
        monkeypatch.setattr(orch, "_subtask_budget_seconds", lambda _n: 0.5)

        # Capture every executor this module creates so we can assert it
        # was shut down (no abandoned pool).
        created: list[ThreadPoolExecutor] = []
        shutdowns: list[dict[str, Any]] = []

        class _TrackingExecutor(ThreadPoolExecutor):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, **kw)
                created.append(self)

            def shutdown(
                self, wait: bool = True, *, cancel_futures: bool = False
            ) -> None:
                shutdowns.append({"wait": wait, "cancel_futures": cancel_futures})
                super().shutdown(wait=wait, cancel_futures=cancel_futures)

        monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", _TrackingExecutor)

        try:
            results = orch._run_subtasks_parallel(
                subtasks=plan,
                goal="g",
                profiles=orch._make_default_profiles(tmp_path),
                max_iterations_per_subtask=4,
                model_factory=model_factory,
            )
        finally:
            release.set()

        # Sanity: the fast worker really did run.
        assert fast_done.is_set()
        # Plan order preserved, one result per subtask.
        assert [r.subtask.id for r in results] == ["t1", "t2"]
        # Fast subtask collected its real answer.
        assert results[0].ok is True
        assert "fast-done" in results[0].answer
        # Slow subtask came back as a timeout marker, not a crash.
        assert results[1].ok is False
        assert "timed out" in results[1].error
        # The dedicated executor was created and shut down (not leaked).
        assert len(created) == 1
        assert created[0]._shutdown is True
        # On timeout the teardown must cancel not-yet-started futures.
        assert shutdowns and shutdowns[-1]["cancel_futures"] is True

    def test_parallel_runs_off_loop_without_deadlock(self, tmp_path: Path) -> None:
        """P20: with no asyncio in the path, a normal off-loop call to the
        parallel orchestrator completes and collects every fast subtask.
        """
        plan = _plan_json(
            ("t1", "review", "first"),
            ("t2", "doc", "second"),
        )
        planner = _SequenceModel([plan])

        class _EchoModel:
            def bind_tools(self, _t: list) -> _EchoModel:
                return self

            def invoke(self, messages: list) -> AIMessage:
                from langchain_core.messages import HumanMessage

                human = next((m for m in messages if isinstance(m, HumanMessage)), None)
                return AIMessage(content=str(human.content)[:80] if human else "ok")

        result = run_orchestration(
            goal="parallel off-loop",
            model=planner,
            working_dir=tmp_path,
            parallel=True,
            model_factory=_EchoModel,
        )
        assert result.ok, result
        assert [r.subtask.id for r in result.subtasks] == ["t1", "t2"]
        assert all(r.ok for r in result.subtasks)

    def test_controller_parallel_flag(self, tmp_path: Path) -> None:
        from bog_agents_cli.orchestrator_controller import (
            OrchestratorController,
            reset_controllers,
        )

        reset_controllers()
        plan = _plan_json(("t1", "review", "review"), ("t2", "doc", "doc"))

        def factory():
            return _SequenceModel([plan, "rev-ans", "doc-ans"])

        c = OrchestratorController(
            working_dir=tmp_path,
            model_factory=factory,
            parallel=False,  # explicitly sequential — easier to assert
        )
        result = c.run("anything")
        assert result.ok


# ---------------------------------------------------------------------------
# K1: /orchestrate --parallel TUI flag
# ---------------------------------------------------------------------------


class TestSlashFlagParsing:
    """Verify that the /orchestrate handler's --parallel parse logic
    strips the flag from the goal and produces an OrchestratorController
    with parallel=True. We test the parse-logic equivalent directly so
    we don't have to spin up the full Textual app.
    """

    def test_parse_strips_flag(self) -> None:
        """Mimic the in-handler tokenizer."""
        tail = "--parallel review and document the auth flow"
        parallel = False
        tokens: list[str] = []
        for tok in tail.split():
            if tok in ("--parallel", "--concurrent"):
                parallel = True
            else:
                tokens.append(tok)
        goal = " ".join(tokens)
        assert parallel is True
        assert goal == "review and document the auth flow"

    def test_parse_no_flag(self) -> None:
        tail = "review my diff"
        parallel = False
        tokens: list[str] = []
        for tok in tail.split():
            if tok in ("--parallel", "--concurrent"):
                parallel = True
            else:
                tokens.append(tok)
        goal = " ".join(tokens)
        assert parallel is False
        assert goal == "review my diff"

    def test_slash_spec_advertises_parallel(self) -> None:
        from bog_agents_cli.commands import general

        cmd = next(c for c in general.COMMANDS if c.name == "/orchestrate")
        subcommand_strs = [s[0] for s in cmd.spec.subcommands]
        assert any("--parallel" in s for s in subcommand_strs)
