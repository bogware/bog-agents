"""Tests for upstream deepagents parity behavior in graph assembly."""

from __future__ import annotations

from typing import Any

import bog_agents.graph as bog_graph
from tests.unit_tests.chat_model import GenericFakeChatModel


class _DummyCompiledGraph:
    """Capture `.with_config()` calls without building a real graph."""

    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    def with_config(self, config: dict[str, Any]) -> _DummyCompiledGraph:
        self.config = config
        return self


def test_create_agent_inherits_interrupts_and_adds_async_subagents(monkeypatch) -> None:
    """Graph assembly should mirror key upstream deepagents subagent behavior."""
    captured: dict[str, object] = {}

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        captured["model"] = model
        captured["kwargs"] = kwargs
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)

    model = GenericFakeChatModel(messages=iter([]))
    result = bog_graph.create_agent(
        model=model,
        name="leader",
        interrupt_on={"edit_file": True},
        subagents=[
            {
                "name": "researcher",
                "description": "Research things",
                "system_prompt": "Research.",
                "model": model,
                "tools": [],
            },
            {
                "name": "remote-worker",
                "description": "Remote worker",
                "graph_id": "remote-graph",
                "url": "https://example.test",
            },
        ],
    )

    middleware = captured["kwargs"]["middleware"]
    subagent_middleware = next(item for item in middleware if isinstance(item, bog_graph.SubAgentMiddleware))
    async_middleware = next(item for item in middleware if isinstance(item, bog_graph.AsyncSubAgentMiddleware))

    assert async_middleware is not None
    assert len(async_middleware.tools) == 5

    subagents = subagent_middleware._subagents
    general_purpose = next(spec for spec in subagents if spec["name"] == "general-purpose")
    researcher = next(spec for spec in subagents if spec["name"] == "researcher")

    assert general_purpose["interrupt_on"] == {"edit_file": True}
    assert researcher["interrupt_on"] == {"edit_file": True}

    assert result.config is not None
    assert result.config["recursion_limit"] == 9999
    assert result.config["metadata"]["versions"]["bog-agents"] == bog_graph.__version__
    assert result.config["metadata"]["lc_agent_name"] == "leader"
