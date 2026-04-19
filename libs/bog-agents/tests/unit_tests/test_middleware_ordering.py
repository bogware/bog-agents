"""Tests for middleware dependency graph enforcement (Item 12).

Verifies that:
- `_validate_middleware_ordering` raises `ValueError` when a required middleware
  appears after (or is absent from) the middleware that depends on it.
- Validation passes when requirements are satisfied in the correct order.
- `ResultSynthesisMiddleware.requires` contains `ParallelWorktreeMiddleware`.
- `create_agent()` raises `ValueError` at build-time for bad ordering.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT
from typing_extensions import TypedDict

from bog_agents import create_agent
from bog_agents.graph import _validate_middleware_ordering
from bog_agents.middleware.result_synthesis import ResultSynthesisMiddleware
from bog_agents.middleware.worktree import ParallelWorktreeMiddleware

# ---------------------------------------------------------------------------
# Minimal stub middleware classes for testing
# ---------------------------------------------------------------------------


class _AState(TypedDict):
    pass


class _BState(TypedDict):
    pass


class _MiddlewareA(AgentMiddleware[_AState, ContextT, ResponseT]):
    """Stub middleware with no requirements."""

    requires: ClassVar[list[type[AgentMiddleware]]] = []
    state_schema = _AState
    tools: ClassVar[list] = []


class _MiddlewareB(AgentMiddleware[_BState, ContextT, ResponseT]):
    """Stub middleware that requires _MiddlewareA to appear first."""

    requires: ClassVar[list[type[AgentMiddleware]]] = [_MiddlewareA]
    state_schema = _BState
    tools: ClassVar[list] = []


class _MiddlewareC(AgentMiddleware[_BState, ContextT, ResponseT]):
    """Stub middleware with no requirements."""

    requires: ClassVar[list[type[AgentMiddleware]]] = []
    state_schema = _BState
    tools: ClassVar[list] = []


# ---------------------------------------------------------------------------
# Unit tests for _validate_middleware_ordering
# ---------------------------------------------------------------------------


class TestValidateMiddlewareOrdering:
    def test_empty_list_passes(self):
        """Validating an empty middleware list must not raise."""
        _validate_middleware_ordering([])

    def test_single_middleware_no_requires_passes(self):
        """A single middleware with no requirements must pass."""
        _validate_middleware_ordering([_MiddlewareA()])

    def test_correct_order_passes(self):
        """A followed by B (which requires A) must pass."""
        _validate_middleware_ordering([_MiddlewareA(), _MiddlewareB()])

    def test_correct_order_with_extra_middleware_passes(self):
        """Unrelated middleware in between must not affect validation."""
        _validate_middleware_ordering([_MiddlewareA(), _MiddlewareC(), _MiddlewareB()])

    def test_wrong_order_raises(self):
        """B before A (B requires A) must raise ValueError."""
        with pytest.raises(ValueError, match="_MiddlewareB requires _MiddlewareA"):
            _validate_middleware_ordering([_MiddlewareB(), _MiddlewareA()])

    def test_missing_requirement_raises(self):
        """B without A anywhere in the list must raise ValueError."""
        with pytest.raises(ValueError, match="_MiddlewareB requires _MiddlewareA"):
            _validate_middleware_ordering([_MiddlewareC(), _MiddlewareB()])

    def test_error_message_includes_current_order(self):
        """The error message must include the current middleware order."""
        with pytest.raises(ValueError, match="Current order:"):
            _validate_middleware_ordering([_MiddlewareB()])

    def test_no_requires_attribute_treated_as_empty(self):
        """Middleware without a `requires` attribute must be treated as having none."""

        class _NoRequires(AgentMiddleware):
            tools: ClassVar[list] = []

        _validate_middleware_ordering([_NoRequires()])

    def test_multiple_independent_middleware_passes(self):
        """Multiple middleware with no dependencies must all pass."""
        _validate_middleware_ordering([_MiddlewareA(), _MiddlewareC(), _MiddlewareA()])


# ---------------------------------------------------------------------------
# Tests for ResultSynthesisMiddleware.requires
# ---------------------------------------------------------------------------


class TestResultSynthesisRequires:
    def test_requires_is_declared(self):
        """ResultSynthesisMiddleware must declare a non-empty `requires` list."""
        assert hasattr(ResultSynthesisMiddleware, "requires")
        assert len(ResultSynthesisMiddleware.requires) > 0

    def test_requires_parallel_worktree(self):
        """ResultSynthesisMiddleware must require ParallelWorktreeMiddleware."""
        assert ParallelWorktreeMiddleware in ResultSynthesisMiddleware.requires

    def test_valid_order_passes(self, tmp_path: Path):
        """ParallelWorktreeMiddleware before ResultSynthesisMiddleware must pass."""
        pwm = ParallelWorktreeMiddleware(working_dir=tmp_path)
        rsm = ResultSynthesisMiddleware()
        _validate_middleware_ordering([pwm, rsm])

    def test_invalid_order_raises(self, tmp_path: Path):
        """ResultSynthesisMiddleware before ParallelWorktreeMiddleware must raise."""
        pwm = ParallelWorktreeMiddleware(working_dir=tmp_path)
        rsm = ResultSynthesisMiddleware()
        with pytest.raises(ValueError, match="ResultSynthesisMiddleware requires ParallelWorktreeMiddleware"):
            _validate_middleware_ordering([rsm, pwm])

    def test_result_synthesis_without_parallel_raises(self):
        """ResultSynthesisMiddleware alone (no ParallelWorktreeMiddleware) must raise."""
        rsm = ResultSynthesisMiddleware()
        with pytest.raises(ValueError, match="ResultSynthesisMiddleware requires ParallelWorktreeMiddleware"):
            _validate_middleware_ordering([rsm])


# ---------------------------------------------------------------------------
# Integration: create_agent raises on bad ordering via the middleware= kwarg
# ---------------------------------------------------------------------------


MODEL = "claude-sonnet-4-20250514"


class TestCreateAgentValidation:
    def test_create_agent_raises_on_bad_middleware_order(self):
        """create_agent() must raise ValueError for unsatisfied middleware requirements."""
        rsm = ResultSynthesisMiddleware()
        with pytest.raises(ValueError, match="ResultSynthesisMiddleware requires ParallelWorktreeMiddleware"):
            create_agent(model=MODEL, middleware=[rsm])

    def test_create_agent_passes_with_correct_middleware_order(self, tmp_path: Path):
        """create_agent() must succeed when middleware dependencies are satisfied."""
        pwm = ParallelWorktreeMiddleware(working_dir=tmp_path)
        rsm = ResultSynthesisMiddleware(parallel_middleware=pwm)
        agent = create_agent(model=MODEL, middleware=[pwm, rsm])
        assert agent is not None
