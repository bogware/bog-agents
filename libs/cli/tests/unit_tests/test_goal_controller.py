"""Unit tests for the /goal + /rubric surface.

Covers the file-backed goal controller (set/show/update round-trip), the
injected-invoke rubric drafter (draft + regenerate-on-feedback), the slash
command registration, and that GoalToolsMiddleware is wired into the built CLI
agent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from bog_agents_cli import goal_controller
from bog_agents_cli.goal_rubric import RubricPending, draft_criteria

# ---------------------------------------------------------------------------
# goal_controller — persistence + round-trip
# ---------------------------------------------------------------------------


def test_set_objective_show_round_trip(tmp_path: Path) -> None:
    record = goal_controller.set_objective(tmp_path, "Ship the goal feature")
    assert record.objective == "Ship the goal feature"
    assert record.status == "active"

    reloaded = goal_controller.load_goal(tmp_path)
    assert reloaded.objective == "Ship the goal feature"
    assert reloaded.is_set
    assert "Ship the goal feature" in goal_controller.render_goal(reloaded)


def test_load_goal_missing_returns_empty(tmp_path: Path) -> None:
    record = goal_controller.load_goal(tmp_path)
    assert not record.is_set
    assert record.rubric == []
    assert "No goal set" in goal_controller.render_goal(record)


def test_set_rubric_round_trip(tmp_path: Path) -> None:
    goal_controller.set_objective(tmp_path, "Do the thing")
    record = goal_controller.set_rubric(tmp_path, ["tests pass", "no new lint"])
    assert record.rubric == ["tests pass", "no new lint"]

    reloaded = goal_controller.load_goal(tmp_path)
    assert reloaded.rubric == ["tests pass", "no new lint"]
    rendered = goal_controller.render_rubric(reloaded)
    assert "tests pass" in rendered
    assert "no new lint" in rendered


def test_update_status_and_note(tmp_path: Path) -> None:
    goal_controller.set_objective(tmp_path, "Refactor module")
    record = goal_controller.set_status(tmp_path, "blocked", note="waiting on review")
    assert record.status == "blocked"
    assert record.note == "waiting on review"

    reloaded = goal_controller.load_goal(tmp_path)
    assert reloaded.status == "blocked"
    assert reloaded.note == "waiting on review"


def test_unknown_status_normalizes_to_active(tmp_path: Path) -> None:
    goal_controller.set_objective(tmp_path, "x")
    record = goal_controller.set_status(tmp_path, "banana")
    assert record.status == "active"


def test_clear_goal(tmp_path: Path) -> None:
    goal_controller.set_objective(tmp_path, "temp goal")
    assert goal_controller.goal_path(tmp_path).exists()
    goal_controller.clear_goal(tmp_path)
    assert not goal_controller.goal_path(tmp_path).exists()
    assert not goal_controller.load_goal(tmp_path).is_set


def test_set_objective_reactivates_completed_goal(tmp_path: Path) -> None:
    goal_controller.set_objective(tmp_path, "first")
    goal_controller.set_status(tmp_path, "complete")
    record = goal_controller.set_objective(tmp_path, "second")
    assert record.status == "active"


# ---------------------------------------------------------------------------
# goal_controller — parsing, state seed, merge
# ---------------------------------------------------------------------------


def test_parse_rubric_lines_strips_bullets_and_numbers() -> None:
    text = "- tests pass\n* 2 no lint\n1. handles errors\n2) is documented"
    parsed = goal_controller.parse_rubric_lines(text)
    assert parsed == ["tests pass", "2 no lint", "handles errors", "is documented"]


def test_parse_rubric_lines_semicolons() -> None:
    assert goal_controller.parse_rubric_lines("a; b ; c") == ["a", "b", "c"]


def test_state_seed_mirrors_record() -> None:
    record = goal_controller.GoalRecord(
        objective="obj", rubric=["c1"], status="blocked", note="n"
    )
    seed = goal_controller.state_seed(record)
    assert seed["_goal_objective"] == "obj"
    assert seed["_goal_rubric"] == ["c1"]
    assert seed["_goal_status"] == "blocked"
    assert seed["_goal_note"] == "n"


def test_state_seed_empty_goal_is_none() -> None:
    seed = goal_controller.state_seed(goal_controller.GoalRecord())
    assert seed["_goal_objective"] is None
    assert seed["_goal_rubric"] is None
    assert seed["_goal_status"] is None


def test_merge_agent_state_folds_status_and_note() -> None:
    record = goal_controller.GoalRecord(objective="obj", status="active")
    merged = goal_controller.merge_agent_state(
        record, {"_goal_status": "complete", "_goal_note": "done, tests green"}
    )
    assert merged.status == "complete"
    assert merged.note == "done, tests green"
    # Original is untouched.
    assert record.status == "active"


def test_merge_agent_state_ignores_none() -> None:
    record = goal_controller.GoalRecord(objective="obj", status="blocked")
    merged = goal_controller.merge_agent_state(record, None)
    assert merged.status == "blocked"


# ---------------------------------------------------------------------------
# goal_rubric — injected-invoke drafting + regenerate gate
# ---------------------------------------------------------------------------


async def test_draft_criteria_from_stub_invoke() -> None:
    captured: dict[str, str] = {}

    async def _invoke(system: str, user: str) -> str:
        captured["system"] = system
        captured["user"] = user
        return "- criterion one\n- criterion two"

    criteria = await draft_criteria("Build a widget", invoke=_invoke)
    assert criteria == ["criterion one", "criterion two"]
    assert "Build a widget" in captured["user"]
    assert "acceptance criteria" in captured["system"].lower()


async def test_draft_criteria_empty_objective_skips_invoke() -> None:
    called = False

    async def _invoke(_system: str, _user: str) -> str:
        nonlocal called
        called = True
        return "- x"

    criteria = await draft_criteria("   ", invoke=_invoke)
    assert criteria == []
    assert called is False


async def test_draft_criteria_regenerates_on_feedback() -> None:
    seen_users: list[str] = []

    async def _invoke(_system: str, user: str) -> str:
        seen_users.append(user)
        if "user_feedback" in user:
            return "- stricter criterion"
        return "- initial criterion"

    first = await draft_criteria("Objective", invoke=_invoke)
    assert first == ["initial criterion"]

    regenerated = await draft_criteria(
        "Objective",
        invoke=_invoke,
        feedback="be stricter about tests",
        previous_criteria=first,
    )
    assert regenerated == ["stricter criterion"]
    # The regenerate prompt carries the feedback and the rejected criteria.
    assert "be stricter about tests" in seen_users[1]
    assert "initial criterion" in seen_users[1]


def test_rubric_pending_dataclass() -> None:
    pending = RubricPending(objective="o", criteria=["a", "b"])
    assert pending.objective == "o"
    assert pending.criteria == ["a", "b"]
    assert pending.created_at > 0


# ---------------------------------------------------------------------------
# Registration + middleware wiring
# ---------------------------------------------------------------------------


def test_goal_and_rubric_specs_registered() -> None:
    from bog_agents_cli.command_registry import get_slash_commands

    names = {name for name, _desc, _kw in get_slash_commands()}
    assert "/goal" in names
    assert "/rubric" in names


def test_goal_and_rubric_have_dispatcher_entries() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert COMMAND_HANDLER_MAP.get("/goal") == "_handle_goal_command"
    assert COMMAND_HANDLER_MAP.get("/rubric") == "_handle_rubric_command"


def test_goal_headless_twin_registered(tmp_path: Path) -> None:
    from bog_agents_cli.headless_commands import HEADLESS_COMMANDS

    assert "goal" in HEADLESS_COMMANDS
    _desc, handler = HEADLESS_COMMANDS["goal"]
    goal_controller.set_objective(tmp_path, "headless goal")
    with patch("bog_agents_cli.headless_commands.Path.cwd", return_value=tmp_path):
        result = handler("")
    assert result.ok
    assert "headless goal" in result.text
    assert result.data is not None
    assert result.data["objective"] == "headless goal"


def test_goal_tools_middleware_in_built_agent(tmp_path: Path) -> None:
    from bog_agents.middleware import GoalToolsMiddleware

    from bog_agents_cli.agent import create_cli_agent

    fake_model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    fake_model.profile = {"max_input_tokens": 200000}

    captured: dict[str, object] = {}

    def _fake_create_agent(*_args: object, **kwargs: object) -> Mock:
        captured["middleware"] = kwargs.get("middleware")
        return Mock()

    with (
        patch(
            "bog_agents_cli.config.create_model",
            return_value=Mock(model=fake_model),
        ),
        patch("bog_agents_cli.agent.get_system_prompt", return_value=""),
        patch("bog_agents_cli.agent.create_agent", side_effect=_fake_create_agent),
    ):
        create_cli_agent(
            model="fake-model",
            assistant_id="test",
            enable_memory=False,
            enable_skills=False,
            enable_shell=False,
            interactive=False,
            cwd=tmp_path,
        )

    middleware = captured["middleware"]
    assert isinstance(middleware, list)
    assert any(isinstance(m, GoalToolsMiddleware) for m in middleware)
