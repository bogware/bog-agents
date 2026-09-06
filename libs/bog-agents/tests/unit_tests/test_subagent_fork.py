"""ROADMAP #71: fork-mode subagents are seeded with the parent's conversation."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from bog_agents.graph import create_agent
from bog_agents.middleware.subagents import seed_fork_messages
from bog_agents.token_audit import RecordingChatModel
from tests.unit_tests.chat_model import GenericFakeChatModel


class TestSeedForkMessages:
    def test_drops_the_pending_tool_call_and_keeps_the_rest(self) -> None:
        parent = [
            HumanMessage(content="hello"),
            AIMessage(content="hi", tool_calls=[{"name": "ls", "args": {}, "id": "c1", "type": "tool_call"}]),
            ToolMessage(content="a.py", tool_call_id="c1"),
            AIMessage(content="I see a.py"),
            HumanMessage(content="now delegate"),
            AIMessage(
                content="", tool_calls=[{"name": "task", "args": {"description": "x", "subagent_type": "scout"}, "id": "c2", "type": "tool_call"}]
            ),
        ]
        seeded = seed_fork_messages(parent, "look at a.py")
        assert [type(m).__name__ for m in seeded] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage", "HumanMessage", "HumanMessage"]
        assert seeded[-1].content == "look at a.py"
        assert not (isinstance(seeded[-2], AIMessage) and seeded[-2].tool_calls)

    def test_system_messages_are_not_duplicated(self) -> None:
        parent = [SystemMessage(content="parent prompt"), HumanMessage(content="hello")]
        seeded = seed_fork_messages(parent, "task")
        assert [type(m).__name__ for m in seeded] == ["HumanMessage", "HumanMessage"]

    def test_empty_parent(self) -> None:
        assert [m.content for m in seed_fork_messages([], "task")] == ["task"]


class TestForkSubagent:
    def test_fork_child_sees_the_parent_conversation_before_the_task(self) -> None:
        child = RecordingChatModel()
        parent_model = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {"description": "summarise what we know", "subagent_type": "scout"},
                                "id": "call_1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        )
        agent = create_agent(
            model=parent_model,
            system_prompt="parent prompt",
            subagents=[{"name": "scout", "description": "forked scout", "system_prompt": "child prompt", "mode": "fork", "model": child}],
        )
        result = agent.invoke({"messages": [HumanMessage(content="we found a bug in a.py")]})

        assert child.calls, "the fork child was never invoked"
        first = child.calls[0]["messages"]
        human_texts = [m.content for m in first if isinstance(m, HumanMessage)]
        assert human_texts[0] == "we found a bug in a.py"
        assert human_texts[-1] == "summarise what we know"
        assert not any(isinstance(m, AIMessage) and m.tool_calls for m in first)
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert tool_messages and tool_messages[-1].tool_call_id == "call_1"

    def test_isolated_child_only_sees_the_task(self) -> None:
        child = RecordingChatModel()
        parent_model = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": "task", "args": {"description": "just this", "subagent_type": "scout"}, "id": "call_1", "type": "tool_call"}
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        )
        agent = create_agent(
            model=parent_model,
            system_prompt="parent prompt",
            subagents=[{"name": "scout", "description": "isolated scout", "system_prompt": "child prompt", "model": child}],
        )
        agent.invoke({"messages": [HumanMessage(content="context the child must not see")]})
        first = child.calls[0]["messages"]
        assert [m.content for m in first if isinstance(m, HumanMessage)] == ["just this"]

    def test_builtin_fork_subagent_uses_the_parent_prompt(self) -> None:
        from bog_agents.feature_config import FeatureConfig
        from bog_agents.middleware.subagents import SubAgentMiddleware
        from bog_agents.token_audit import capture_assembly

        captured: dict[str, list[object]] = {}
        with capture_assembly(lambda a: captured.update(mw=list(a.middleware))):
            create_agent(model=RecordingChatModel(), system_prompt="THE PARENT PROMPT")
        sub_mw = next(m for m in captured["mw"] if isinstance(m, SubAgentMiddleware))
        specs = {s["name"]: s for s in sub_mw._subagent_specs}
        assert "fork" in specs and "general-purpose" in specs
        fork_raw = specs["fork"]["raw"]
        assert fork_raw["mode"] == "fork"
        assert "THE PARENT PROMPT" in str(fork_raw["system_prompt"])
        assert "THE PARENT PROMPT" not in str(specs["general-purpose"]["raw"]["system_prompt"])

        captured.clear()
        with capture_assembly(lambda a: captured.update(mw=list(a.middleware))):
            create_agent(model=RecordingChatModel(), config=FeatureConfig(enable_fork_subagent=False))
        sub_mw = next(m for m in captured["mw"] if isinstance(m, SubAgentMiddleware))
        assert all(s["name"] != "fork" for s in sub_mw._subagent_specs)
