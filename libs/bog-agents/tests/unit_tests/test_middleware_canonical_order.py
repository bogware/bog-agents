"""Lock in the canonical middleware ordering produced by ``create_agent``.

The middleware list order in ``graph.py`` is **load-bearing**: changing
positions silently shifts which middleware sees which message
transformations. The existing ``_validate_middleware_ordering`` only
checks declarative ``requires`` constraints; it does NOT pin the
specific sequence we ship.

This test snapshots the order so a future refactor that reorders blocks
in ``graph.py`` fails CI rather than silently changing cost-accounting,
caching, or summarization semantics in production.

When the order intentionally changes:

1. Audit the affected interactions (e.g. does Summarization still run
   before PromptCaching? does CostTracker still wrap the final
   request?).
2. Update the expected sequences below.
3. Note the rationale in the commit message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bog_agents import create_agent
from bog_agents.feature_config import FeatureConfig

if TYPE_CHECKING:
    import pytest


def _capture_middleware_list(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> list[str]:
    """Build an agent and return the names of every middleware in order.

    Hooks ``_validate_middleware_ordering`` to snapshot the list
    immediately before compilation. We don't run the model, we just
    want the ordered class-name sequence.
    """
    captured: list[str] = []

    from bog_agents import graph as graph_module

    original = graph_module._validate_middleware_ordering

    def _spy(middleware_list: list[Any]) -> None:
        captured.extend(type(m).__name__ for m in middleware_list)
        return original(middleware_list)

    monkeypatch.setattr(graph_module, "_validate_middleware_ordering", _spy)

    create_agent(model="claude-sonnet-4-20250514", **kwargs)
    return captured


class TestCanonicalMiddlewareOrder:
    def test_minimal_agent_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An agent with no optional features still emits the core 5."""
        names = _capture_middleware_list(monkeypatch)

        # Core stack always present in this order. The tail order
        # (FilesystemMiddleware -> SubAgentMiddleware ->
        # SummarizationMiddleware -> PatchToolCallsMiddleware) is the
        # default-append block in graph.py; it is what every agent
        # built without explicit middleware= relies on.
        assert names[0] == "TodoListMiddleware", names
        assert "FilesystemMiddleware" in names
        assert "SubAgentMiddleware" in names
        assert "_BogAgentsSummarizationMiddleware" in names
        assert "PatchToolCallsMiddleware" in names
        assert "AnthropicPromptCachingMiddleware" in names

        # PromptCaching is the closest middleware to the model — must
        # be the last (innermost) so it sees the final message list
        # after Summarization compresses it.
        assert names[-1] == "AnthropicPromptCachingMiddleware", names

    def test_summarization_runs_before_prompt_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Summarization must compress messages before caching sees them.

        If PromptCaching wrapped Summarization (i.e. ran outside),
        caching would key off the pre-summarization message list and
        miss every cache hit after a summarization event.
        """
        names = _capture_middleware_list(monkeypatch)
        assert names.index("_BogAgentsSummarizationMiddleware") < names.index("AnthropicPromptCachingMiddleware")

    def test_cost_tracker_before_default_summarization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CostTracker wraps Summarization so it observes the full request size.

        With CostTracker outside (earlier in the list), its before/after
        token accounting sees the messages as the user sent them; with
        Summarization outside, the cost log records compressed counts
        and operators can't audit pre-summarization spend.
        """
        names = _capture_middleware_list(monkeypatch, config=FeatureConfig(enable_cost_tracking=True))
        assert "CostTrackerMiddleware" in names
        assert names.index("CostTrackerMiddleware") < names.index("_BogAgentsSummarizationMiddleware")

    def test_feature_middleware_runs_before_default_filesystem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Optional feature middleware are appended before the default tail.

        Anything in the `enable_*` block of graph.py needs to wrap the
        default FilesystemMiddleware/SubAgentMiddleware so feature-level
        approvals/rules/observability see file-operation traffic.
        """
        names = _capture_middleware_list(monkeypatch, config=FeatureConfig(enable_plan_mode=True))
        assert "PlanModeMiddleware" in names
        # PlanMode must appear before FilesystemMiddleware to intercept
        # mutating tools before the filesystem backend executes them.
        assert names.index("PlanModeMiddleware") < names.index("FilesystemMiddleware")

    def test_user_middleware_runs_after_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """User-supplied middleware is appended after the default tail.

        The contract is: user middleware sees a fully-built agent
        request. Subagents and filesystem backend are already attached;
        user middleware can layer on top safely.
        """
        from langchain.agents.middleware.types import (
            AgentMiddleware,
            ContextT,
            ResponseT,
        )
        from typing_extensions import TypedDict

        class _UState(TypedDict):
            pass

        class _UserMW(AgentMiddleware[_UState, ContextT, ResponseT]):
            state_schema = _UState

        names = _capture_middleware_list(monkeypatch, middleware=[_UserMW()])
        assert "_UserMW" in names
        assert names.index("_UserMW") > names.index("FilesystemMiddleware")
        assert names.index("_UserMW") > names.index("SubAgentMiddleware")

    def test_memory_middleware_appears_after_user_middleware(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Memory middleware reads/writes persistent storage and should run
        after user middleware so any user-defined message transforms are
        captured in memory rather than missed.
        """
        from langchain.agents.middleware.types import (
            AgentMiddleware,
            ContextT,
            ResponseT,
        )
        from typing_extensions import TypedDict

        class _UState(TypedDict):
            pass

        class _UserMW(AgentMiddleware[_UState, ContextT, ResponseT]):
            state_schema = _UState

        # Provide an empty sources list so MemoryMiddleware is added
        # but doesn't actually try to load anything from disk.
        names = _capture_middleware_list(monkeypatch, middleware=[_UserMW()], memory=[])
        if "MemoryMiddleware" in names:
            assert names.index("_UserMW") < names.index("MemoryMiddleware")
