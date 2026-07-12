"""Unit tests for `GoalToolsMiddleware`.

Covers the goal/rubric round-trip through the exposed tools, the completion
gate that defers to `RubricMiddleware`'s verdict, per-turn system-prompt
injection of the live goal, cross-turn state persistence, and the
`PrivateStateAttr` contract that keeps goal bookkeeping out of the public I/O
schema.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from bog_agents.middleware._private_state import private_state_field_names
from bog_agents.middleware.goal_tools import (
    GOAL_TOOLS_SYSTEM_PROMPT,
    GoalState,
    GoalToolsMiddleware,
    _goal_snapshot,
    _render_goal_context,
    _rubric_snapshot,
    _update_goal_command,
)

try:
    from langchain.agents.middleware.types import ModelRequest
except ImportError:  # pragma: no cover - import-path fallback
    from langchain.agents.middleware import ModelRequest  # type: ignore[no-redef,attr-defined]


class _FakeModel:
    """Minimal BaseChatModel stand-in: identifiable, never actually called."""

    _llm_type = "fake"

    def _get_ls_params(self, **_kwargs: Any) -> dict[str, str]:
        return {"ls_provider": "fake", "ls_model_name": "fake-model"}


def _make_request(state: dict[str, Any]) -> ModelRequest:
    """Build a `ModelRequest` carrying `state` and a base system prompt."""
    return ModelRequest(
        model=_FakeModel(),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="base system prompt"),
        tools=[],
        runtime=None,
        state={"messages": [HumanMessage(content="hi")], **state},
    )


def _passthrough(request: ModelRequest) -> Any:
    """Sync handler that records the request it received."""
    _passthrough.last_request = request  # type: ignore[attr-defined]
    return None


async def _apassthrough(request: ModelRequest) -> Any:
    """Async handler that records the request it received."""
    _passthrough.last_request = request  # type: ignore[attr-defined]
    return None


def _system_text(request: ModelRequest) -> str:
    """Flatten a request's system message content to plain text."""
    sm = request.system_message
    if sm is None:
        return ""
    if isinstance(sm.content, str):
        return sm.content
    return " ".join(b.get("text", "") for b in sm.content if isinstance(b, dict) and b.get("type") == "text")


def _tool(mw: GoalToolsMiddleware, name: str) -> BaseTool:
    """Return the middleware tool with the given name."""
    for tool in mw.tools:
        if tool.name == name:
            return tool
    msg = f"tool {name!r} not found in {[t.name for t in mw.tools]}"
    raise AssertionError(msg)


def _apply(state: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge a `Command` update's non-message keys into a state dict."""
    for key, value in update.items():
        if key == "messages":
            continue
        state[key] = value
    return state


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


def test_exposes_three_tools_with_expected_names() -> None:
    mw = GoalToolsMiddleware()
    names = {t.name for t in mw.tools}
    assert names == {"get_goal", "get_rubric", "update_goal"}


# ---------------------------------------------------------------------------
# Round-trip: set a goal then read it back
# ---------------------------------------------------------------------------


def test_get_update_round_trip_through_tools() -> None:
    mw = GoalToolsMiddleware()
    get_goal = _tool(mw, "get_goal")
    update_goal = _tool(mw, "update_goal")

    state: dict[str, Any] = {}
    command = update_goal.func(  # type: ignore[attr-defined]
        tool_call_id="c1",
        state=state,
        objective="Ship the goal tools middleware",
        rubric=["tests pass", "ruff clean"],
    )
    _apply(state, command.update)

    snapshot = get_goal.func(state)  # type: ignore[attr-defined]
    assert snapshot["objective"] == "Ship the goal tools middleware"
    assert snapshot["criteria"] == ["tests pass", "ruff clean"]
    # A set goal defaults to active when no status was supplied.
    assert snapshot["status"] == "active"
    assert snapshot["active"] is True
    assert snapshot["note"] is None


def test_get_rubric_reports_goal_criteria_and_grading_status() -> None:
    state = {"_goal_rubric": ["a", "b"], "_rubric_status": "needs_revision"}
    snapshot = _rubric_snapshot(state)
    assert snapshot["active"] is True
    assert snapshot["criteria"] == ["a", "b"]
    assert snapshot["grading_status"] == "needs_revision"


def test_get_rubric_falls_back_to_public_rubric_input() -> None:
    # No goal rubric set; the public RubricMiddleware `rubric` string is split.
    snapshot = _rubric_snapshot({"rubric": "line one\nline two\n"})
    assert snapshot["criteria"] == ["line one", "line two"]


def test_empty_goal_snapshot_is_inactive() -> None:
    snapshot = _goal_snapshot({})
    assert snapshot["active"] is False
    assert snapshot["objective"] is None
    assert snapshot["status"] is None
    assert snapshot["criteria"] == []


# ---------------------------------------------------------------------------
# update_goal status transitions + completion gate
# ---------------------------------------------------------------------------


def test_update_blocked_records_status_and_note() -> None:
    state = {"_goal_objective": "do the thing"}
    command = _update_goal_command(
        objective=None,
        rubric=None,
        status="blocked",
        note="waiting on credentials",
        tool_call_id="c1",
        state=state,
    )
    assert command.update["_goal_status"] == "blocked"
    assert command.update["_goal_note"] == "waiting on credentials"
    _apply(state, command.update)
    snapshot = _goal_snapshot(state)
    assert snapshot["status"] == "blocked"
    assert snapshot["active"] is True  # blocked is still unfinished
    assert snapshot["note"] == "waiting on credentials"


def test_complete_refused_when_rubric_not_satisfied() -> None:
    state = {"_goal_objective": "do the thing", "_rubric_status": "needs_revision"}
    command = _update_goal_command(
        objective=None,
        rubric=None,
        status="complete",
        note=None,
        tool_call_id="c1",
        state=state,
    )
    # Nothing committed: only a rejection message is returned.
    assert "_goal_status" not in command.update
    assert list(command.update.keys()) == ["messages"]
    assert "not 'satisfied'" in command.update["messages"][0].content


def test_complete_allowed_when_rubric_satisfied() -> None:
    state = {"_goal_objective": "do the thing", "_rubric_status": "satisfied"}
    command = _update_goal_command(
        objective=None,
        rubric=None,
        status="complete",
        note="all criteria met",
        tool_call_id="c1",
        state=state,
    )
    assert command.update["_goal_status"] == "complete"
    _apply(state, command.update)
    assert _goal_snapshot(state)["active"] is False


def test_complete_allowed_when_no_rubric_graded() -> None:
    state = {"_goal_objective": "do the thing"}
    command = _update_goal_command(
        objective=None,
        rubric=None,
        status="complete",
        note=None,
        tool_call_id="c1",
        state=state,
    )
    assert command.update["_goal_status"] == "complete"


def test_empty_update_returns_message_only() -> None:
    command = _update_goal_command(
        objective=None,
        rubric=None,
        status=None,
        note=None,
        tool_call_id="c1",
        state={},
    )
    assert list(command.update.keys()) == ["messages"]
    assert "Nothing to update" in command.update["messages"][0].content


# ---------------------------------------------------------------------------
# System-prompt injection (the goal stays in view each turn)
# ---------------------------------------------------------------------------


def test_render_goal_context_includes_static_guidance_when_no_goal() -> None:
    text = _render_goal_context({})
    assert text == GOAL_TOOLS_SYSTEM_PROMPT


def test_wrap_model_call_injects_goal_and_preserves_base_prompt() -> None:
    mw = GoalToolsMiddleware()
    request = _make_request(
        {
            "_goal_objective": "land the feature",
            "_goal_rubric": ["ci green"],
            "_goal_status": "active",
            "_goal_note": "started",
        }
    )
    mw.wrap_model_call(request, _passthrough)
    text = _system_text(_passthrough.last_request)  # type: ignore[attr-defined]
    assert "base system prompt" in text
    assert "Goal and Rubric" in text  # from GOAL_TOOLS_SYSTEM_PROMPT
    assert "land the feature" in text
    assert "ci green" in text
    assert "started" in text


async def test_awrap_model_call_injects_goal() -> None:
    mw = GoalToolsMiddleware()
    request = _make_request({"_goal_objective": "async objective"})
    await mw.awrap_model_call(request, _apassthrough)
    assert "async objective" in _system_text(_passthrough.last_request)  # type: ignore[attr-defined]


def test_wrap_model_call_with_no_goal_still_injects_guidance() -> None:
    mw = GoalToolsMiddleware()
    request = _make_request({})
    mw.wrap_model_call(request, _passthrough)
    text = _system_text(_passthrough.last_request)  # type: ignore[attr-defined]
    assert "base system prompt" in text
    assert "Goal and Rubric" in text


# ---------------------------------------------------------------------------
# Cross-turn persistence
# ---------------------------------------------------------------------------


def test_state_persists_across_turns() -> None:
    mw = GoalToolsMiddleware()
    update_goal = _tool(mw, "update_goal")
    get_goal = _tool(mw, "get_goal")

    state: dict[str, Any] = {}
    # Turn 1: set the goal.
    _apply(state, update_goal.func(tool_call_id="c1", state=state, objective="multi-turn goal").update)  # type: ignore[attr-defined]
    # Turn 2: add a note; the objective must survive.
    _apply(state, update_goal.func(tool_call_id="c2", state=state, note="progress made").update)  # type: ignore[attr-defined]

    snapshot = get_goal.func(state)  # type: ignore[attr-defined]
    assert snapshot["objective"] == "multi-turn goal"
    assert snapshot["note"] == "progress made"


# ---------------------------------------------------------------------------
# Private-state contract
# ---------------------------------------------------------------------------


def test_goal_channels_are_private() -> None:
    private = private_state_field_names(GoalState)
    assert {"_goal_objective", "_goal_rubric", "_goal_status", "_goal_note"} <= private


def test_goal_channels_absent_from_input_schema() -> None:
    mw = GoalToolsMiddleware()
    # The middleware's declared state schema must be GoalState, and none of the
    # private goal channels may appear in the public input schema LangGraph
    # derives from it.
    assert mw.state_schema is GoalState
    input_fields = set(getattr(GoalState.__mro__[0], "__annotations__", {}))
    # Sanity: the private channels are declared on GoalState itself...
    assert "_goal_objective" in input_fields
    # ...but they are private, so private_state_field_names withholds them.
    assert "_goal_objective" in private_state_field_names(GoalState)


# ---------------------------------------------------------------------------
# Lazy-import health
# ---------------------------------------------------------------------------


def test_lazy_export_resolves() -> None:
    import bog_agents.middleware as m

    assert m.GoalToolsMiddleware is GoalToolsMiddleware
    assert m.GoalState is GoalState


def test_goal_tools_not_eagerly_imported() -> None:
    """Importing the middleware package must not pull in `goal_tools`."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        import bog_agents.middleware  # noqa: F401
        loaded = [m for m in sys.modules if m == 'bog_agents.middleware.goal_tools']
        print(len(loaded))
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "0", result.stdout
