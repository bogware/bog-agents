"""Tests for `private_state_field_names` private-state-key introspection."""

from typing import Annotated, NotRequired, TypedDict

from langchain.agents.middleware.types import PrivateStateAttr

from bog_agents.middleware._private_state import private_state_field_names
from bog_agents.middleware.memory import MemoryState
from bog_agents.middleware.rubric import RubricState
from bog_agents.middleware.skills import SkillsState
from bog_agents.middleware.summarization import SummarizationState


class _BothOrders(TypedDict):
    """Exercises both nesting orders that appear in real bog-agents schemas."""

    outer_notrequired: NotRequired[Annotated[int, PrivateStateAttr]]
    outer_annotated: Annotated[NotRequired[str], PrivateStateAttr]
    plain: str
    plain_optional: NotRequired[int]
    annotated_but_not_private: Annotated[int, "some other metadata"]


def test_detects_both_nesting_orders() -> None:
    assert private_state_field_names(_BothOrders) == frozenset({"outer_notrequired", "outer_annotated"})


def test_plain_fields_are_not_returned() -> None:
    private = private_state_field_names(_BothOrders)
    assert "plain" not in private
    assert "plain_optional" not in private
    assert "annotated_but_not_private" not in private


def test_container_of_private_type_is_not_flagged() -> None:
    """A private *element* type must not make the containing key private."""

    class _Container(TypedDict):
        holder: NotRequired[dict[str, Annotated[int, PrivateStateAttr]]]

    assert private_state_field_names(_Container) == frozenset()


def test_rubric_state_private_keys() -> None:
    private = private_state_field_names(RubricState)
    assert {
        "_rubric_status",
        "_rubric_iterations",
        "_rubric_evaluations",
        "_current_grading_run_id",
        "_active_rubric",
    } <= private
    # `rubric` is the public knob callers write; it must stay in the I/O schema.
    assert "rubric" not in private
    assert "messages" not in private


def test_summarization_state_private_keys() -> None:
    # Declared as `Annotated[NotRequired[...], PrivateStateAttr]` -- the inverted order.
    assert "_summarization_event" in private_state_field_names(SummarizationState)


def test_skills_and_memory_state_private_keys() -> None:
    assert "skills_metadata" in private_state_field_names(SkillsState)
    assert "memory_contents" in private_state_field_names(MemoryState)


def test_multiple_schemas_union() -> None:
    private = private_state_field_names(RubricState, SummarizationState, SkillsState, MemoryState)
    assert {"_rubric_status", "_summarization_event", "skills_metadata", "memory_contents"} <= private


def test_no_schemas_returns_empty() -> None:
    assert private_state_field_names() == frozenset()


def test_unresolvable_schema_is_skipped_not_raised() -> None:
    class _Broken(TypedDict):
        bad: "NotAThing"  # noqa: F821  # unresolvable forward ref

    assert private_state_field_names(_Broken, RubricState) >= frozenset({"_rubric_status"})
