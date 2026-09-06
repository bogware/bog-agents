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
        # SummarizationMiddleware -> PatchToolCallsMiddleware ->
        # OutputTruncationMiddleware) is the default-append block in
        # graph.py; it is what every agent built without explicit
        # middleware= relies on.
        assert names[0] == "TodoListMiddleware", names
        assert "FilesystemMiddleware" in names
        assert "SubAgentMiddleware" in names
        assert "_BogAgentsSummarizationMiddleware" in names
        assert "PatchToolCallsMiddleware" in names
        assert "OutputTruncationMiddleware" in names
        assert "AnthropicPromptCachingMiddleware" in names

        # PromptCaching is the closest middleware to the model — must
        # be the last (innermost) so it sees the final message list
        # after Summarization compresses it. The tail contract is
        # ``[AnthropicPromptCachingMiddleware]`` OR, for a Bedrock model
        # with ``langchain-aws`` installed,
        # ``[AnthropicPromptCachingMiddleware, BedrockPromptCachingMiddleware]``.
        # This suite builds a NON-Bedrock model (claude-sonnet-4), so the
        # Bedrock entry must never be appended and Anthropic caching stays
        # strictly last — pinning that the non-Bedrock stack is unchanged.
        assert "BedrockPromptCachingMiddleware" not in names, names
        assert names[-1] == "AnthropicPromptCachingMiddleware", names

    def test_summarization_runs_before_prompt_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Summarization must compress messages before caching sees them.

        If PromptCaching wrapped Summarization (i.e. ran outside),
        caching would key off the pre-summarization message list and
        miss every cache hit after a summarization event.
        """
        names = _capture_middleware_list(monkeypatch)
        assert names.index("_BogAgentsSummarizationMiddleware") < names.index("AnthropicPromptCachingMiddleware")

    def test_output_truncation_is_innermost_of_summarization_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OutputTruncation wraps inside PatchToolCalls but outside prompt caching.

        It sits after PatchToolCalls (argument truncation) and Summarization
        (compaction) so a continuation re-invokes only the raw model call, yet
        stays outside PromptCaching so every cached prefix is still tagged.
        """
        names = _capture_middleware_list(monkeypatch)
        assert names.index("PatchToolCallsMiddleware") < names.index("OutputTruncationMiddleware")
        assert names.index("OutputTruncationMiddleware") < names.index("AnthropicPromptCachingMiddleware")

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

    def test_street_sweeper_position(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Street sweeper wraps inside CostTracker and outside Summarization.

        It only edits message *content* (never count/order), so it must run
        before the default SummarizationMiddleware (whose cutoff indices then
        stay aligned) and before PromptCaching (whose prefix it must not
        invalidate by running afterward). CostTracker stays outermost so its
        accounting still observes the pre-sweep request.
        """
        names = _capture_middleware_list(
            monkeypatch,
            config=FeatureConfig(enable_street_sweeper=True, enable_cost_tracking=True),
        )
        assert "StreetSweeperMiddleware" in names
        assert names.index("CostTrackerMiddleware") < names.index("StreetSweeperMiddleware")
        assert names.index("StreetSweeperMiddleware") < names.index("_BogAgentsSummarizationMiddleware")
        assert names.index("StreetSweeperMiddleware") < names.index("AnthropicPromptCachingMiddleware")

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

    def test_user_middleware_replaces_builtin_at_original_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A user middleware colliding with a built-in `.name` replaces it in place.

        Upstream REPLACE semantics: rather than keep-first dedup dropping the
        user's instance, a `.name` collision swaps the user's middleware into
        the built-in's ORIGINAL slot. A stack built with only the default
        middleware plus a same-named override must therefore keep exactly one
        instance of that name, positioned where the built-in sat (before the
        prompt-caching tail), not appended at the very end.
        """
        from langchain.agents.middleware.types import AgentMiddleware

        class _FakeSubAgent(AgentMiddleware):
            name = "SubAgentMiddleware"

        names = _capture_middleware_list(monkeypatch, middleware=[_FakeSubAgent()])
        assert names.count("_FakeSubAgent") == 1, names
        # The built-in `SubAgentMiddleware` slot was taken over (its class name
        # no longer appears), and the replacement sits before PromptCaching.
        assert "SubAgentMiddleware" not in names, names
        assert names.index("_FakeSubAgent") < names.index("AnthropicPromptCachingMiddleware"), names

    def test_subagent_middleware_omitted_when_general_purpose_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The GP-disabled stack legitimately omits SubAgentMiddleware.

        When the active harness profile disables the general-purpose subagent
        and no synchronous `subagents=` are supplied, there is nothing to back
        the `task` tool, so `SubAgentMiddleware` (normally part of the default
        tail) is not installed at all.
        """
        from bog_agents import graph as graph_module
        from bog_agents.profiles.harness.harness_profiles import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
        )

        gp_disabled = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
        monkeypatch.setattr(graph_module, "_harness_profile_for_model", lambda *a, **k: gp_disabled)

        names = _capture_middleware_list(monkeypatch)
        assert "SubAgentMiddleware" not in names, names
        # The rest of the core tail is unaffected.
        assert "FilesystemMiddleware" in names, names
        assert "_BogAgentsSummarizationMiddleware" in names, names

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

    def test_memory_runs_before_prompt_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Memory must run BEFORE PromptCaching (V3-2).

        Memory.modify_request appends a new system content block; PromptCaching
        tags the *last* system block with cache_control. If Memory ran after
        caching, the injected memory text would fall outside the cached prefix.
        Memory must therefore be the outer (earlier) middleware.
        """
        names = _capture_middleware_list(monkeypatch, memory=[])
        assert "MemoryMiddleware" in names, names
        assert names.index("MemoryMiddleware") < names.index("AnthropicPromptCachingMiddleware"), names

    def test_dlp_runs_before_audit_trail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DLP must run BEFORE AuditTrail (V3-3, compliance hazard).

        DLP redacts sensitive values on the inbound path; AuditTrail records the
        request. If Audit ran first (outer), it would log unredacted secrets.
        DLP must be the outer (earlier) middleware so the audit trail captures
        the redacted request.
        """
        names = _capture_middleware_list(
            monkeypatch,
            config=FeatureConfig(enable_dlp=True, enable_audit_trail=True),
        )
        assert "DLPMiddleware" in names, names
        assert "AuditTrailMiddleware" in names, names
        assert names.index("DLPMiddleware") < names.index("AuditTrailMiddleware"), names


def test_custom_street_sweeper_replaces_builtin_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """v6 SDK-3: a caller-supplied sweeper takes the canonical slot, not the tail.

    The CLI attaches a long-lived `StreetSweeperMiddleware` singleton through
    `middleware=`. With `enable_street_sweeper=True` the built-in instance is
    replaced *in place* by name, so the singleton runs inside CostTracker and
    outside Summarization exactly like the FeatureConfig route. Without the
    flag it would be spliced after the core stack (inside summarization).
    """
    from bog_agents import graph as graph_module
    from bog_agents.middleware.street_sweeper import StreetSweeperMiddleware

    captured: list[Any] = []
    original = graph_module._validate_middleware_ordering

    def _spy(middleware_list: list[Any]) -> None:
        captured.extend(middleware_list)
        return original(middleware_list)

    monkeypatch.setattr(graph_module, "_validate_middleware_ordering", _spy)
    mine = StreetSweeperMiddleware(enabled=False)
    create_agent(
        model="claude-sonnet-4-20250514",
        config=FeatureConfig(enable_street_sweeper=True, enable_cost_tracking=True),
        middleware=[mine],
    )
    sweepers = [m for m in captured if isinstance(m, StreetSweeperMiddleware)]
    assert sweepers == [mine], "the built-in sweeper must be replaced by the caller's instance, not kept alongside it"
    names = [type(m).__name__ for m in captured]
    assert names.index("CostTrackerMiddleware") < names.index("StreetSweeperMiddleware")
    assert names.index("StreetSweeperMiddleware") < names.index("_BogAgentsSummarizationMiddleware")
