"""Tests for the AgentBuilder fluent API."""

from __future__ import annotations

from bog_agents.builder import AgentBuilder
from bog_agents.middleware.parallel_agents import ParallelAgentsMiddleware


def test_with_multi_agent_wires_parallel_agents_middleware(monkeypatch) -> None:
    """with_multi_agent() wires the live ParallelAgentsMiddleware.

    The in-process orchestrator was removed in V1, so the builder must no
    longer forward the dead ``enable_multi_agent`` / ``max_agent_threads``
    flags — it adds the ``parallel_tasks`` middleware instead.
    """
    captured: dict = {}

    def fake_create_agent(**kwargs: object) -> str:
        captured.update(kwargs)
        return "GRAPH"

    monkeypatch.setattr("bog_agents.graph.create_agent", fake_create_agent)

    result = AgentBuilder("anthropic:claude-sonnet-4-6").with_multi_agent().build()

    assert result == "GRAPH"
    middleware = captured.get("middleware", [])
    assert any(isinstance(m, ParallelAgentsMiddleware) for m in middleware)
    # The dead orchestrator flags must no longer be forwarded.
    assert "enable_multi_agent" not in captured
    assert "max_agent_threads" not in captured


def test_build_without_multi_agent_omits_parallel_middleware(monkeypatch) -> None:
    captured: dict = {}

    def fake_create_agent(**kwargs: object) -> str:
        captured.update(kwargs)
        return "GRAPH"

    monkeypatch.setattr("bog_agents.graph.create_agent", fake_create_agent)

    AgentBuilder("anthropic:claude-sonnet-4-6").build()

    middleware = captured.get("middleware", [])
    assert not any(isinstance(m, ParallelAgentsMiddleware) for m in middleware)
