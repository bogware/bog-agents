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


def _capture_system_prompt(monkeypatch, **create_kwargs: Any) -> str:
    """Build an agent with a stubbed backend and return the assembled system prompt."""
    captured: dict[str, object] = {}

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        captured["system_prompt"] = kwargs["system_prompt"]
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)
    model = GenericFakeChatModel(messages=iter([]))
    bog_graph.create_agent(model=model, **create_kwargs)
    return captured["system_prompt"]  # type: ignore[return-value]


def test_system_prompt_plain_string_prepends_base(monkeypatch) -> None:
    """A bare string lands before the base prompt (historical `prefix` behavior)."""
    prompt = _capture_system_prompt(monkeypatch, system_prompt="CALLER-PREFIX")
    assert isinstance(prompt, str)
    assert prompt.startswith("CALLER-PREFIX")
    assert bog_graph.BASE_AGENT_PROMPT in prompt


def test_system_prompt_config_base_none_drops_base(monkeypatch) -> None:
    """`base: None` (key present, value None) drops the default base entirely."""
    prompt = _capture_system_prompt(
        monkeypatch,
        system_prompt={"prefix": "PFX", "base": None, "suffix": "SFX"},
    )
    assert prompt == "PFX\n\nSFX"
    assert bog_graph.BASE_AGENT_PROMPT not in prompt


def test_system_prompt_config_suffix_lands_after_base(monkeypatch) -> None:
    """An omitted `base` key keeps the default base; `suffix` lands after it."""
    prompt = _capture_system_prompt(monkeypatch, system_prompt={"suffix": "TRAILING"})
    assert bog_graph.BASE_AGENT_PROMPT in prompt
    assert prompt.index(bog_graph.BASE_AGENT_PROMPT) < prompt.index("TRAILING")


def test_system_prompt_config_replaces_base(monkeypatch) -> None:
    """A present `base` value replaces the default base; ordering is prefix->base->suffix."""
    prompt = _capture_system_prompt(
        monkeypatch,
        system_prompt={"prefix": "P", "base": "B", "suffix": "S"},
    )
    assert prompt == "P\n\nB\n\nS"
    assert bog_graph.BASE_AGENT_PROMPT not in prompt


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
    assert result.config["recursion_limit"] == 200  # default max_turns=200
    # Upstream parity: the langsmith metadata key is `lc_versions`, not the
    # bog-local `versions` this test previously asserted (a bug it locked in).
    assert result.config["metadata"]["lc_versions"]["bog-agents"] == bog_graph.__version__
    assert result.config["metadata"]["lc_agent_name"] == "leader"


def test_recursion_limit_is_not_clamped_at_1000(monkeypatch) -> None:
    """`max_turns` above 1000 is honored (the old hard clamp was a bug).

    Upstream deepagents defaults `recursion_limit` to 9,999 and does not clamp;
    bog keeps its native `max_turns` default (200) but no longer caps the value
    at 1000. This test previously would have failed because
    `max(10, min(max_turns, 1000))` pinned any large value to 1000.
    """

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)

    model = GenericFakeChatModel(messages=iter([]))
    result = bog_graph.create_agent(model=model, max_turns=5000)

    assert result.config is not None
    assert result.config["recursion_limit"] == 5000


def test_user_middleware_replaces_colliding_builtin_in_place(monkeypatch) -> None:
    """A user middleware whose `.name` collides with a built-in REPLACES it.

    Upstream parity: rather than keep-first dedup dropping the user's instance,
    the collision replaces the built-in at its ORIGINAL stack position. Here a
    user-supplied middleware named `SubAgentMiddleware` takes over the built-in
    `SubAgentMiddleware` slot (same index), and the user's instance is the one
    handed to `create_agent`.
    """
    captured: dict[str, object] = {}

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        captured["kwargs"] = kwargs
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)

    from langchain.agents.middleware.types import AgentMiddleware

    class _FakeSubAgent(AgentMiddleware):
        name = "SubAgentMiddleware"

    user_mw = _FakeSubAgent()
    model = GenericFakeChatModel(messages=iter([]))
    bog_graph.create_agent(model=model, middleware=[user_mw])

    middleware = list(captured["kwargs"]["middleware"])
    names = [m.name for m in middleware]
    # Exactly one entry named SubAgentMiddleware, and it is the user's instance
    # (the built-in was replaced in place, not appended after).
    assert names.count("SubAgentMiddleware") == 1
    subagent_entry = next(m for m in middleware if m.name == "SubAgentMiddleware")
    assert subagent_entry is user_mw
    # The replacement sits where the built-in SubAgentMiddleware was — before
    # the tail (PromptCaching), not appended at the very end.
    assert names.index("SubAgentMiddleware") < names.index("AnthropicPromptCachingMiddleware")


def test_task_tool_dropped_when_general_purpose_disabled(monkeypatch) -> None:
    """Disabling the general-purpose subagent with no other subagents drops SubAgentMiddleware.

    With `GeneralPurposeSubagentProfile(enabled=False)` on the active harness
    profile and no synchronous `subagents=`, `all_subagents` is empty, so the
    `task`-tool backend (`SubAgentMiddleware`) is not installed at all.
    """
    captured: dict[str, object] = {}

    def fake_create_agent(model: object, **kwargs: object) -> _DummyCompiledGraph:
        captured["kwargs"] = kwargs
        return _DummyCompiledGraph()

    monkeypatch.setattr(bog_graph, "_langchain_create_agent", fake_create_agent)

    from bog_agents.profiles.harness.harness_profiles import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
    )

    gp_disabled = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))
    monkeypatch.setattr(bog_graph, "_harness_profile_for_model", lambda *a, **k: gp_disabled)

    model = GenericFakeChatModel(messages=iter([]))
    bog_graph.create_agent(model=model)

    names = [type(m).__name__ for m in captured["kwargs"]["middleware"]]
    assert "SubAgentMiddleware" not in names, names
