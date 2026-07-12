"""Goal tools middleware: a persistent, agent-visible goal and rubric.

`GoalToolsMiddleware` gives the agent a durable *objective* plus
*acceptance criteria* (a rubric) that survive across turns and are
re-injected into the system prompt on every model call, so the agent
never loses sight of what it is working toward.

It exposes three tools -- `get_goal`, `get_rubric`, and `update_goal` --
and stores the goal in [`PrivateStateAttr`][langchain.agents.middleware.types.PrivateStateAttr]-marked
state channels so the bookkeeping never leaks into the agent's public
input/output schema.

Completion is *not* self-graded here: `update_goal(status="complete")`
defers to `RubricMiddleware`'s verdict channel (`_rubric_status`) rather
than forking its grader. When a rubric has been graded and the verdict is
not `satisfied`, a completion request is refused with actionable feedback.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    TypedDict,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    ResponseT,
)
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from bog_agents.middleware._utils import append_to_system_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

GoalStatus = Literal["active", "blocked", "complete"]
"""Lifecycle status of the persistent goal.

- `active`: the goal is set and unfinished (the default for a set goal).
- `blocked`: the agent cannot proceed without user input.
- `complete`: the acceptance criteria are satisfied.
"""

_GOAL_STATUSES: frozenset[str] = frozenset({"active", "blocked", "complete"})
"""Recognized `GoalStatus` values, used to normalize persisted state."""

GOAL_TOOLS_SYSTEM_PROMPT = """## Goal and Rubric

You have a persistent goal with acceptance criteria that outlive this turn.
Use `get_goal` to re-read the objective, status, and criteria before deciding
what to do next, and `get_rubric` to inspect the acceptance criteria and the
latest grading status. Use `update_goal` to record progress: mark the goal
`blocked` when you need the user, or `complete` only when the acceptance
criteria are met. A completion request is rejected while the rubric verdict is
anything other than satisfied."""
"""Model-visible guidance injected before each request by `GoalToolsMiddleware`.

The live goal (objective, status, criteria, latest note) is appended to this
static preamble each turn so the goal stays in view without a tool call.
"""


class GoalSnapshot(TypedDict):
    """Read-only goal view returned by the `get_goal` tool to the model."""

    active: bool
    """Whether the goal is unfinished. `False` when no goal is set or when the
    status is `complete`."""

    objective: str | None
    """Active goal objective, or `None` when no goal is set."""

    status: GoalStatus | None
    """Lifecycle status, or `None` when no goal is set. A set goal with an
    unrecognized persisted status is normalized to `active`."""

    criteria: list[str]
    """Acceptance criteria for the goal. Empty when no rubric is set."""

    note: str | None
    """Latest progress or blocker note recorded via `update_goal`."""


class RubricSnapshot(TypedDict):
    """Read-only rubric view returned by the `get_rubric` tool to the model."""

    active: bool
    """Whether acceptance criteria are currently available."""

    criteria: list[str]
    """Current acceptance criteria. Empty when no rubric is set."""

    grading_status: str | None
    """Latest `RubricMiddleware` grading status for the in-progress or
    just-completed graded turn, or `None`.

    Owned by the SDK's `RubricMiddleware` when it is co-composed into the same
    graph; `None` when no rubric has been graded yet.
    """


class GoalState(AgentState):
    """State schema for `GoalToolsMiddleware`.

    None of the goal fields are part of the public I/O schema: every channel is
    annotated with
    [`PrivateStateAttr`][langchain.agents.middleware.types.PrivateStateAttr] so
    the goal bookkeeping is omitted from the agent's input/output schemas.
    Consumers (the CLI `/goal` and `/rubric` commands, evals, observability)
    reach these channels through checkpointed state
    (`agent.get_state(config).values`) or by writing state updates.
    """

    _goal_objective: NotRequired[Annotated[str | None, PrivateStateAttr]]
    """The persistent goal objective. Private; not in I/O schema."""

    _goal_rubric: NotRequired[Annotated[list[str] | None, PrivateStateAttr]]
    """Acceptance criteria for the goal, as a list of criterion strings.
    Private; not in I/O schema."""

    _goal_status: NotRequired[Annotated[GoalStatus | None, PrivateStateAttr]]
    """Lifecycle status of the goal. Private; not in I/O schema."""

    _goal_note: NotRequired[Annotated[str | None, PrivateStateAttr]]
    """Latest progress or blocker note. Private; not in I/O schema."""


def _clean_text(value: Any) -> str | None:
    """Return a non-empty stripped string from `value`, or `None`.

    Args:
        value: Candidate value from state or a tool argument.

    Returns:
        The stripped string when it is a non-empty `str`, otherwise `None`.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _clean_criteria(value: Any) -> list[str]:
    """Return a cleaned list of criterion strings from `value`.

    Args:
        value: Candidate rubric value (expected to be a list of strings).

    Returns:
        Non-empty stripped criterion strings, in order. Empty when `value` is
        not a list or contains no usable entries.
    """
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _coerce_status(value: Any) -> GoalStatus | None:
    """Normalize a persisted status value to a known `GoalStatus`.

    Args:
        value: Candidate status from state.

    Returns:
        The recognized `GoalStatus`, or `None` when `value` is unset. An
        unrecognized truthy value is normalized to `active` so a bogus status
        never leaks to the model.
    """
    text = _clean_text(value)
    if text is None:
        return None
    if text in _GOAL_STATUSES:
        return text  # type: ignore[return-value]
    return "active"


def _rubric_criteria(state: dict[str, Any]) -> list[str]:
    """Resolve the active acceptance criteria from state.

    Prefers the goal's own `_goal_rubric` list; falls back to the public
    `RubricMiddleware` `rubric` string input (split into lines) when no goal
    rubric is set.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        The active acceptance criteria, or an empty list when none are set.
    """
    criteria = _clean_criteria(state.get("_goal_rubric"))
    if criteria:
        return criteria
    rubric_input = _clean_text(state.get("rubric"))
    if rubric_input is None:
        return []
    return [line.strip() for line in rubric_input.splitlines() if line.strip()]


def _rubric_snapshot(state: dict[str, Any]) -> RubricSnapshot:
    """Build the `get_rubric` response from graph state.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        Rubric snapshot visible to the model.
    """
    criteria = _rubric_criteria(state)
    grading_status = _clean_text(state.get("_rubric_status"))
    return {
        "active": bool(criteria),
        "criteria": criteria,
        "grading_status": grading_status,
    }


def _goal_snapshot(state: dict[str, Any]) -> GoalSnapshot:
    """Build the `get_goal` response from graph state.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        Goal snapshot visible to the model.
    """
    objective = _clean_text(state.get("_goal_objective"))
    criteria = _rubric_criteria(state)
    if objective is None:
        return {
            "active": False,
            "objective": None,
            "status": None,
            "criteria": criteria,
            "note": None,
        }
    status: GoalStatus = _coerce_status(state.get("_goal_status")) or "active"
    return {
        # A goal is active until it is complete; `blocked` is still unfinished.
        "active": status != "complete",
        "objective": objective,
        "status": status,
        "criteria": criteria,
        "note": _clean_text(state.get("_goal_note")),
    }


def _render_goal_context(state: dict[str, Any]) -> str:
    """Render the goal guidance plus the live goal for system-prompt injection.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        The static goal guidance, with the current objective, status, criteria,
        and latest note appended when a goal is set.
    """
    parts = [GOAL_TOOLS_SYSTEM_PROMPT]
    snapshot = _goal_snapshot(state)
    if snapshot["objective"] is not None:
        lines = [
            f"Current goal: {snapshot['objective']}",
            f"Status: {snapshot['status']}",
        ]
        if snapshot["criteria"]:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {criterion}" for criterion in snapshot["criteria"])
        if snapshot["note"]:
            lines.append(f"Latest note: {snapshot['note']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _tool_message(content: str, tool_call_id: str) -> Command[Any]:
    """Return a `Command` carrying only a `ToolMessage` reply.

    Args:
        content: Text of the tool reply.
        tool_call_id: Tool call ID the reply answers.

    Returns:
        A `Command` that appends the `ToolMessage` and mutates nothing else.
    """
    return Command(update={"messages": [ToolMessage(content=content, tool_call_id=tool_call_id)]})


def _update_goal_command(
    *,
    objective: str | None,
    rubric: list[str] | None,
    status: GoalStatus | None,
    note: str | None,
    tool_call_id: str,
    state: dict[str, Any],
) -> Command[Any]:
    """Build the `update_goal` state-mutating command.

    Applies only the fields the model supplied. A `complete` status is gated on
    the `RubricMiddleware` verdict channel (`_rubric_status`): when a rubric has
    been graded and its verdict is not `satisfied`, completion is refused and
    nothing is committed.

    Args:
        objective: New objective, or `None` to leave it unchanged.
        rubric: New acceptance criteria, or `None` to leave them unchanged.
        status: New lifecycle status, or `None` to leave it unchanged.
        note: New progress or blocker note, or `None` to leave it unchanged.
        tool_call_id: Tool call ID for the returned `ToolMessage`.
        state: Current graph state injected by LangGraph.

    Returns:
        A `Command` that updates the supplied goal fields and returns a tool
        message, or a message-only `Command` when nothing could be committed.
    """
    updates: dict[str, Any] = {}
    changed: list[str] = []

    clean_objective = _clean_text(objective)
    if clean_objective is not None:
        updates["_goal_objective"] = clean_objective
        changed.append("objective")

    if rubric is not None:
        updates["_goal_rubric"] = _clean_criteria(rubric)
        changed.append("rubric")

    clean_note = _clean_text(note)
    if clean_note is not None:
        updates["_goal_note"] = clean_note
        changed.append("note")

    if status is not None:
        if status == "complete":
            rubric_status = _clean_text(state.get("_rubric_status"))
            if rubric_status is not None and rubric_status != "satisfied":
                # Reuse RubricMiddleware's verdict rather than forking a grader:
                # refuse completion until its rubric run reports `satisfied`.
                return _tool_message(
                    f"Cannot mark the goal complete: the latest rubric verdict is "
                    f"'{rubric_status}', not 'satisfied'. Address the grader feedback "
                    f"and let the rubric pass first.",
                    tool_call_id,
                )
        updates["_goal_status"] = status
        changed.append(f"status={status}")

    if not updates:
        return _tool_message(
            "Nothing to update. Provide at least one of objective, rubric, status, or note.",
            tool_call_id,
        )

    updates["messages"] = [ToolMessage(content=f"Goal updated ({', '.join(changed)}).", tool_call_id=tool_call_id)]
    return Command(update=updates)


class GoalToolsMiddleware(AgentMiddleware[GoalState, ContextT, ResponseT]):
    """Expose a persistent goal and rubric to the agent.

    Delivered as an `AgentMiddleware` rather than a stateless tool bundle
    because the feature needs all three things a bundle cannot provide: a
    `state_schema` with `PrivateStateAttr` goal channels, per-call
    system-prompt injection of the live goal (`wrap_model_call`), and tools
    that read and mutate that state via `Command` updates.

    The middleware carries no configuration and is safe to include
    unconditionally: with no goal set, `get_goal`/`get_rubric` report an empty
    goal and the injected prompt is just the static guidance.
    """

    state_schema = GoalState

    def __init__(self) -> None:
        """Initialize the goal tools."""
        super().__init__()

        @tool
        def get_goal(state: Annotated[dict[str, Any], InjectedState]) -> GoalSnapshot:
            """Read the current persistent goal.

            Call this before deciding what to do next to see the objective, the
            current status, the acceptance criteria, and any prior note.

            Returns:
                Goal snapshot with `active`, `objective`, `status`, `criteria`,
                and `note` keys.
            """
            return _goal_snapshot(state)

        @tool
        def get_rubric(state: Annotated[dict[str, Any], InjectedState]) -> RubricSnapshot:
            """Read the acceptance criteria used to judge completion.

            Call this to inspect the active rubric and the latest grading status
            if a graded turn has already run.

            Returns:
                Rubric snapshot with `active`, `criteria`, and `grading_status`
                keys.
            """
            return _rubric_snapshot(state)

        @tool
        def update_goal(
            tool_call_id: Annotated[str, InjectedToolCallId],
            state: Annotated[dict[str, Any], InjectedState],
            objective: str | None = None,
            rubric: list[str] | None = None,
            status: Literal["active", "blocked", "complete"] | None = None,
            note: str | None = None,
        ) -> Command[Any]:
            """Update the persistent goal.

            Supply only the fields you want to change. Use `status="blocked"`
            when you need the user, and `status="complete"` only when the
            acceptance criteria are met -- a completion request is rejected
            while the rubric verdict is anything other than satisfied.

            Args:
                tool_call_id: Injected tool call ID for the tool response.
                state: Injected graph state holding the current goal.
                objective: New objective text, or `None` to leave it unchanged.
                rubric: New acceptance criteria, or `None` to leave them
                    unchanged.
                status: New status (`active`, `blocked`, or `complete`), or
                    `None` to leave it unchanged.
                note: New progress or blocker note, or `None` to leave it
                    unchanged.

            Returns:
                Command that updates the supplied goal fields and returns a tool
                message.
            """
            return _update_goal_command(
                objective=objective,
                rubric=rubric,
                status=status,
                note=note,
                tool_call_id=tool_call_id,
                state=state,
            )

        self.tools = [get_goal, get_rubric, update_goal]

    @staticmethod
    def _request_with_goal_context(request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        """Append the goal guidance and live goal to the request's system prompt.

        Args:
            request: Model request being processed.

        Returns:
            Model request with the goal context appended to the system message.
        """
        state: dict[str, Any] = dict(request.state) if request.state else {}
        context = _render_goal_context(state)
        new_system_message = append_to_system_message(request.system_message, context)
        return request.override(system_message=new_system_message)

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Inject the goal context into each model request.

        Args:
            request: Model request being processed.
            handler: Handler to call with the modified request.

        Returns:
            Model response from the wrapped handler.
        """
        return handler(self._request_with_goal_context(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Inject the goal context into each async model request.

        Args:
            request: Model request being processed.
            handler: Async handler to call with the modified request.

        Returns:
            Model response from the wrapped handler.
        """
        return await handler(self._request_with_goal_context(request))


__all__ = [
    "GOAL_TOOLS_SYSTEM_PROMPT",
    "GoalSnapshot",
    "GoalState",
    "GoalStatus",
    "GoalToolsMiddleware",
    "RubricSnapshot",
]
