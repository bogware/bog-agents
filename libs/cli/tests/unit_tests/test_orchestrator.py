"""Tests for /orchestrate (Wave G — T-8 Roo Boomerang Tasks parity)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.messages import AIMessage

from bog_agents_cli.orchestrator import (
    OrchestrationResult,
    SubtaskMode,
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
        plan, raw, err = decompose_goal("review and document the auth flow", model=model)
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
            [json.dumps({"plan": [{"id": "t1", "mode": "frobnicate", "description": "x"}]})]
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

    def test_subtask_modes_get_distinct_system_prompts(
        self, tmp_path: Path
    ) -> None:
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

        prompts = [m.content for inv in subtask_invocations
                   for m in inv if isinstance(m, SystemMessage)]
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
