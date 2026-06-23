"""Hardening tests for `create_agent` middleware de-duplication (S4).

Covers the case where a feature is supplied both via a convenience kwarg
(`skills=`, `memory=`) and as an explicit middleware instance via `middleware=`,
plus the always-appended `AnthropicPromptCachingMiddleware`. Without the guards,
langchain raises an opaque "Please remove duplicate middleware instances"
`AssertionError`; with them the user's instance is the only one composed.
"""

from __future__ import annotations

from typing import Any

from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

import bog_agents.graph as bog_graph
from bog_agents.backends import StateBackend
from bog_agents.feature_config import FeatureConfig
from bog_agents.graph import _dedup_middleware_by_name
from bog_agents.middleware.memory import MemoryMiddleware
from bog_agents.middleware.result_synthesis import ResultSynthesisMiddleware
from bog_agents.middleware.skills import SkillsMiddleware
from bog_agents.middleware.worktree import ParallelWorktreeMiddleware
from tests.unit_tests.chat_model import GenericFakeChatModel


class _DummyCompiledGraph:
    """Capture `.with_config()` calls without building a real graph."""

    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    def with_config(self, config: dict[str, Any]) -> _DummyCompiledGraph:
        self.config = config
        return self


def _capture_middleware(monkeypatch, **create_kwargs: Any) -> list[Any]:
    """Run `create_agent` with a stubbed langchain backend and return middleware.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        **create_kwargs: Keyword arguments forwarded to `create_agent`.

    Returns:
        The assembled middleware list passed to `_langchain_create_agent`.
    """
    captured: dict[str, Any] = {}

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        captured["kwargs"] = kwargs
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)
    model = GenericFakeChatModel(messages=iter([]))
    bog_graph.create_agent(model=model, **create_kwargs)
    return list(captured["kwargs"]["middleware"])


# ---------------------------------------------------------------------------
# _dedup_middleware_by_name (keep-first backstop)
# ---------------------------------------------------------------------------


class TestDedupHelper:
    def test_keeps_first_of_duplicate_names(self, caplog) -> None:
        """The first instance of each `.name` survives; later twins are dropped."""
        first = SkillsMiddleware(backend=StateBackend, sources=["/a/"])
        second = SkillsMiddleware(backend=StateBackend, sources=["/b/"])
        other = MemoryMiddleware(backend=StateBackend, sources=["/m/"])

        with caplog.at_level("WARNING"):
            result = _dedup_middleware_by_name([first, second, other])

        assert result == [first, other]
        assert result[0] is first
        assert any("duplicate middleware" in rec.message.lower() for rec in caplog.records)

    def test_no_duplicates_is_noop(self) -> None:
        """A list without duplicate names is returned unchanged."""
        a = SkillsMiddleware(backend=StateBackend, sources=["/a/"])
        b = MemoryMiddleware(backend=StateBackend, sources=["/m/"])
        result = _dedup_middleware_by_name([a, b])
        assert result == [a, b]


# ---------------------------------------------------------------------------
# create_agent honors user precedence for convenience-kwarg features (S4)
# ---------------------------------------------------------------------------


class TestCreateAgentDedup:
    def test_skills_kwarg_and_middleware_do_not_double(self, monkeypatch) -> None:
        """`skills=` plus an explicit SkillsMiddleware yields exactly one, the user's."""
        user_skills = SkillsMiddleware(backend=StateBackend, sources=["/user/skills/"])
        middleware = _capture_middleware(monkeypatch, skills=["/kwarg/skills/"], middleware=[user_skills])

        skills_mw = [m for m in middleware if isinstance(m, SkillsMiddleware)]
        assert len(skills_mw) == 1
        assert skills_mw[0] is user_skills

    def test_memory_kwarg_and_middleware_do_not_double(self, monkeypatch) -> None:
        """`memory=` plus an explicit MemoryMiddleware yields exactly one, the user's."""
        user_memory = MemoryMiddleware(backend=StateBackend, sources=["/user/AGENTS.md"])
        middleware = _capture_middleware(monkeypatch, memory=["/kwarg/AGENTS.md"], middleware=[user_memory])

        memory_mw = [m for m in middleware if isinstance(m, MemoryMiddleware)]
        assert len(memory_mw) == 1
        assert memory_mw[0] is user_memory

    def test_user_prompt_caching_not_doubled(self, monkeypatch) -> None:
        """A user-supplied AnthropicPromptCachingMiddleware is not duplicated."""
        user_caching = AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")
        middleware = _capture_middleware(monkeypatch, middleware=[user_caching])

        caching_mw = [m for m in middleware if isinstance(m, AnthropicPromptCachingMiddleware)]
        assert len(caching_mw) == 1
        assert caching_mw[0] is user_caching

    def test_no_duplicate_names_in_assembled_stack(self, monkeypatch) -> None:
        """The fully assembled stack never contains two middleware with the same name."""
        user_skills = SkillsMiddleware(backend=StateBackend, sources=["/user/skills/"])
        user_memory = MemoryMiddleware(backend=StateBackend, sources=["/user/AGENTS.md"])
        user_caching = AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")
        middleware = _capture_middleware(
            monkeypatch,
            skills=["/kwarg/skills/"],
            memory=["/kwarg/AGENTS.md"],
            middleware=[user_skills, user_memory, user_caching],
        )

        names = [m.name for m in middleware]
        assert len(names) == len(set(names)), f"duplicate middleware names: {names}"


# ---------------------------------------------------------------------------
# ResultSynthesis auto-provision asymmetry (V3-24)
# ---------------------------------------------------------------------------


class TestResultSynthesisParallelWorktree:
    """`enable_result_synthesis` + an explicit ParallelWorktreeMiddleware (V3-24).

    Before the fix the result-synthesis block searched only the feature-wired
    stack (user middleware not yet appended), auto-provisioned a second
    ParallelWorktreeMiddleware, and the keep-first dedup pass then silently
    discarded the user's configured instance. These tests assert the build
    succeeds, contains exactly one ParallelWorktreeMiddleware, and that it is
    the user's instance — and that ResultSynthesis is wired to that same
    instance and still sees it earlier in the stack (`requires=` satisfied).
    """

    def test_flag_plus_explicit_parallel_worktree_builds(self, monkeypatch) -> None:
        """The build succeeds with no duplicate ParallelWorktreeMiddleware crash."""
        user_parallel = ParallelWorktreeMiddleware(working_dir=None)
        middleware = _capture_middleware(
            monkeypatch,
            config=FeatureConfig(enable_result_synthesis=True),
            middleware=[user_parallel],
        )

        parallel_mw = [m for m in middleware if isinstance(m, ParallelWorktreeMiddleware)]
        assert len(parallel_mw) == 1, "auto-provisioned a duplicate ParallelWorktreeMiddleware"
        assert parallel_mw[0] is user_parallel, "user's ParallelWorktreeMiddleware was discarded"

        names = [m.name for m in middleware]
        assert len(names) == len(set(names)), f"duplicate middleware names: {names}"

    def test_result_synthesis_wired_to_user_instance_and_ordered(self, monkeypatch) -> None:
        """ResultSynthesis references the user's instance and sees it earlier."""
        user_parallel = ParallelWorktreeMiddleware(working_dir=None)
        middleware = _capture_middleware(
            monkeypatch,
            config=FeatureConfig(enable_result_synthesis=True),
            middleware=[user_parallel],
        )

        synthesis = next(m for m in middleware if isinstance(m, ResultSynthesisMiddleware))
        # Wired to the user's instance, not an auto-provisioned twin.
        assert synthesis._parallel_middleware is user_parallel

        # `requires=` ordering: ParallelWorktreeMiddleware must appear earlier.
        parallel_idx = next(i for i, m in enumerate(middleware) if isinstance(m, ParallelWorktreeMiddleware))
        synthesis_idx = next(i for i, m in enumerate(middleware) if isinstance(m, ResultSynthesisMiddleware))
        assert parallel_idx < synthesis_idx

    def test_flag_alone_auto_provisions_one(self, monkeypatch) -> None:
        """Without a user instance the flag still auto-provisions exactly one."""
        middleware = _capture_middleware(
            monkeypatch,
            config=FeatureConfig(enable_result_synthesis=True),
        )

        parallel_mw = [m for m in middleware if isinstance(m, ParallelWorktreeMiddleware)]
        assert len(parallel_mw) == 1
        synthesis = next(m for m in middleware if isinstance(m, ResultSynthesisMiddleware))
        assert synthesis._parallel_middleware is parallel_mw[0]
