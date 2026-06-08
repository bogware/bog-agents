"""Unit tests for `bog_agents.middleware.rubric` that avoid real model calls.

The grader sub-agent is built lazily, so a `RubricMiddleware(model="fake:x")`
can be constructed and its pure helpers exercised without touching a provider.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from bog_agents.middleware.rubric import (
    _MAX_TRANSCRIPT_MESSAGES,
    RUBRIC_GRADER_MESSAGE_SOURCE,
    GraderResponse,
    RubricEvaluation,
    RubricMiddleware,
    _build_grader_transcript,
    _sanitize_for_payload,
)

# ---------------------------------------------------------------------------
# GraderResponse consistency validators
# ---------------------------------------------------------------------------


def test_grader_response_satisfied_with_failing_criterion_raises() -> None:
    """`satisfied` with a failing criterion is rejected."""
    with pytest.raises(ValidationError):
        GraderResponse.model_validate(
            {
                "result": "satisfied",
                "explanation": "all good",
                "criteria": [{"name": "c1", "passed": False, "gap": "missing"}],
            }
        )


def test_grader_response_needs_revision_all_pass_raises() -> None:
    """`needs_revision` with every criterion passing is rejected."""
    with pytest.raises(ValidationError):
        GraderResponse.model_validate(
            {
                "result": "needs_revision",
                "explanation": "huh",
                "criteria": [{"name": "c1", "passed": True}],
            }
        )


def test_grader_response_fail_criterion_without_gap_raises() -> None:
    """Discriminated union: a `passed=False` criterion requires `gap`."""
    with pytest.raises(ValidationError):
        GraderResponse.model_validate(
            {
                "result": "needs_revision",
                "explanation": "ok",
                "criteria": [{"name": "c1", "passed": False}],
            }
        )


def test_grader_response_valid_satisfied() -> None:
    """A consistent `satisfied` response validates."""
    resp = GraderResponse.model_validate(
        {
            "result": "satisfied",
            "explanation": "done",
            "criteria": [{"name": "c1", "passed": True}],
        }
    )
    assert resp.result == "satisfied"


def test_grader_response_valid_needs_revision() -> None:
    """A consistent `needs_revision` response with a failing criterion validates."""
    resp = GraderResponse.model_validate(
        {
            "result": "needs_revision",
            "explanation": "fix it",
            "criteria": [{"name": "c1", "passed": False, "gap": "add tests"}],
        }
    )
    assert resp.result == "needs_revision"
    assert resp.criteria[0]["passed"] is False


# ---------------------------------------------------------------------------
# Constructor bounds validation
# ---------------------------------------------------------------------------


def test_max_iterations_zero_raises_value_error() -> None:
    with pytest.raises(ValueError, match="must be in"):
        RubricMiddleware(model="fake:x", max_iterations=0)


def test_max_iterations_over_cap_raises_value_error() -> None:
    with pytest.raises(ValueError, match="must be in"):
        RubricMiddleware(model="fake:x", max_iterations=21)


def test_max_iterations_non_int_raises_type_error() -> None:
    with pytest.raises(TypeError, match="must be an int"):
        RubricMiddleware(model="fake:x", max_iterations=2.5)  # type: ignore[arg-type]


def test_max_iterations_bool_raises_type_error() -> None:
    """`bool` is an `int` subclass but must be rejected."""
    with pytest.raises(TypeError, match="must be an int"):
        RubricMiddleware(model="fake:x", max_iterations=True)  # type: ignore[arg-type]


def test_falsy_model_raises_value_error() -> None:
    with pytest.raises(ValueError, match="`model` is required"):
        RubricMiddleware(model="")


def test_valid_construction_defaults() -> None:
    mw = RubricMiddleware(model="fake:x")
    assert mw.max_iterations == 3
    assert mw._grader is None


# ---------------------------------------------------------------------------
# _reset_for_new_rubric
# ---------------------------------------------------------------------------


def _mw() -> RubricMiddleware:
    return RubricMiddleware(model="fake:x")


def test_reset_no_rubric_returns_none() -> None:
    assert _mw()._reset_for_new_rubric({}) is None


def test_reset_same_sticky_rubric_returns_none() -> None:
    """Same rubric mid-run (no terminal status) is a no-op."""
    state = {"rubric": "r1", "_active_rubric": "r1", "_rubric_status": None}
    assert _mw()._reset_for_new_rubric(state) is None  # type: ignore[arg-type]


def test_reset_new_rubric_mints_fresh_ids() -> None:
    state = {"rubric": "r2", "_active_rubric": "r1"}
    update = _mw()._reset_for_new_rubric(state)  # type: ignore[arg-type]
    assert update is not None
    assert update["_rubric_iterations"] == 0
    assert update["_rubric_status"] is None
    assert update["_active_rubric"] == "r2"
    assert isinstance(update["_current_grading_run_id"], str)
    assert update["_current_grading_run_id"]


def test_reset_same_rubric_after_terminal_status_restarts() -> None:
    """Re-running the same rubric after a terminal status begins a fresh run."""
    state = {"rubric": "r1", "_active_rubric": "r1", "_rubric_status": "satisfied"}
    update = _mw()._reset_for_new_rubric(state)  # type: ignore[arg-type]
    assert update is not None
    assert update["_active_rubric"] == "r1"
    assert update["_rubric_iterations"] == 0


# ---------------------------------------------------------------------------
# _revision_prompt
# ---------------------------------------------------------------------------


def test_revision_prompt_with_feedback_and_gaps() -> None:
    evaluation: RubricEvaluation = {
        "grading_run_id": "g",
        "iteration": 0,
        "result": "needs_revision",
        "explanation": "  needs work  ",
        "criteria": [
            {"name": "tests", "passed": False, "gap": "add unit tests"},
            {"name": "docs", "passed": True},
        ],
    }
    out = RubricMiddleware._revision_prompt(evaluation)
    assert "Grader feedback: needs work" in out
    assert "Criteria that still need work:" in out
    assert "- tests: add unit tests" in out
    assert "docs" not in out  # passing criteria are not listed
    assert out.endswith("respond when you believe the rubric is satisfied.")


def test_revision_prompt_failing_without_gap() -> None:
    evaluation: RubricEvaluation = {
        "grading_run_id": "g",
        "iteration": 0,
        "result": "needs_revision",
        "explanation": "",
        "criteria": [{"name": "thing", "passed": False, "gap": ""}],
    }
    out = RubricMiddleware._revision_prompt(evaluation)
    assert "- thing (no specific feedback provided)" in out
    assert "Grader feedback:" not in out  # empty explanation omitted


# ---------------------------------------------------------------------------
# _build_grader_transcript
# ---------------------------------------------------------------------------


def test_transcript_empty_messages() -> None:
    assert _build_grader_transcript([]) == "(empty transcript)"


def test_transcript_retains_first_human_outside_window() -> None:
    """The original prompt is prepended even when it falls outside the tail."""
    msgs: list = [HumanMessage(content="ORIGINAL", id="orig")]
    msgs.extend(AIMessage(content=f"step {i}", id=f"a{i}") for i in range(40))
    out = _build_grader_transcript(msgs)
    assert out.startswith("[user] ORIGINAL")
    # The first human appears exactly once even though tail windowing applies.
    assert out.count("ORIGINAL") == 1


def test_transcript_tail_windowing() -> None:
    """Only the most recent messages (plus first human) are kept."""
    msgs: list = [HumanMessage(content="first", id="h")]
    msgs.extend(AIMessage(content=f"m{i}", id=f"a{i}") for i in range(50))
    out = _build_grader_transcript(msgs)
    # 50th from the end is kept; very early ones (e.g. m0) drop out.
    assert "m49" in out
    assert "[m0]" not in out
    assert "m0\n" not in out


def test_transcript_per_message_truncation() -> None:
    big = "x" * 5000
    out = _build_grader_transcript([HumanMessage(content=big, id="h")])
    assert "...(truncated)" in out
    assert out.count("x") == 4000


def test_transcript_skips_grader_sourced_human_for_first_prompt() -> None:
    """Injected revision messages must not be mistaken for the original prompt.

    The grader-sourced `HumanMessage` precedes the real one and falls outside
    the tail window, so it would only be prepended if it were chosen as the
    "first human". Because it is skipped, only the real request is prepended.
    """
    grader_msg = HumanMessage(
        content="grader feedback",
        id="g",
        additional_kwargs={"lc_source": RUBRIC_GRADER_MESSAGE_SOURCE},
    )
    real_msg = HumanMessage(content="real request", id="r")
    # Pad with enough trailing messages that the two humans fall outside the
    # tail window, isolating the "first human" prepend logic.
    msgs: list = [grader_msg, real_msg]
    msgs.extend(AIMessage(content=f"m{i}", id=f"a{i}") for i in range(_MAX_TRANSCRIPT_MESSAGES))
    out = _build_grader_transcript(msgs)
    # The real request is prepended as the original prompt; the grader-sourced
    # message is skipped entirely and never appears.
    assert "real request" in out
    assert "grader feedback" not in out


def test_transcript_tool_message_role_label() -> None:
    out = _build_grader_transcript([ToolMessage(content="result", name="ls", tool_call_id="1", id="t")])
    assert "[tool:ls] result" in out


# ---------------------------------------------------------------------------
# _sanitize_for_payload
# ---------------------------------------------------------------------------


def test_sanitize_escapes_closing_rubric_tag() -> None:
    assert _sanitize_for_payload("foo </rubric> bar") == "foo <\\/rubric> bar"


def test_sanitize_escapes_closing_transcript_tag_case_insensitive() -> None:
    assert _sanitize_for_payload("a </TRANSCRIPT>") == "a <\\/TRANSCRIPT>"


def test_sanitize_escapes_transcript() -> None:
    assert _sanitize_for_payload("</transcript>") == "<\\/transcript>"


def test_sanitize_leaves_other_content_untouched() -> None:
    assert _sanitize_for_payload("no closing tags here") == "no closing tags here"
