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
from bog_agents.graph import _dedup_middleware_by_name
from bog_agents.middleware.memory import MemoryMiddleware
from bog_agents.middleware.skills import SkillsMiddleware
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
