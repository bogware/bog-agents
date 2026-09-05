"""`CostLedger` / `RunawayCaps` gate the default `task` fan-out path (v6 SDK-7).

Before this, the ledger was consulted only by `teams.run_team`; a model could
fan out any number of `task` calls and `max_subagents` never fired.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from bog_agents import create_agent
from bog_agents.cost_ledger import CapDecision, CostLedger, RunawayCaps
from bog_agents.middleware.subagents import CompiledSubAgent, _cap_refusal
from tests.unit_tests.chat_model import GenericFakeChatModel


def _task_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "add 2 and 3", "subagent_type": "general-purpose"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _parent_and_child(*, ledger: CostLedger | None, calls: int = 1):
    parent_model = GenericFakeChatModel(messages=iter([*[_task_call(f"call_{i}") for i in range(calls)], AIMessage(content="done")]))
    child_model = GenericFakeChatModel(messages=iter([AIMessage(content="The sum is 5.")] * calls))
    child = create_agent(model=child_model)
    return create_agent(
        model=parent_model,
        checkpointer=InMemorySaver(),
        subagents=[CompiledSubAgent(name="general-purpose", description="general", runnable=child)],
        cost_ledger=ledger,
    )


def _tool_messages(result: dict) -> list[str]:
    return [str(m.content) for m in result["messages"] if m.type == "tool"]


def test_spawn_cap_refuses_the_task_call_and_subagent_never_runs() -> None:
    ledger = CostLedger(caps=RunawayCaps(max_subagents=0))
    parent = _parent_and_child(ledger=ledger)

    result = parent.invoke({"messages": [HumanMessage(content="go")]}, config={"configurable": {"thread_id": "t"}})

    tool = _tool_messages(result)
    assert len(tool) == 1
    assert "Cannot spawn subagent `general-purpose`" in tool[0]
    assert "spawn cap reached" in tool[0]
    assert "The sum is 5." not in tool[0]
    assert ledger.subagent_spawns == 0


def test_spawns_are_counted_when_allowed() -> None:
    ledger = CostLedger(caps=RunawayCaps(max_subagents=5))
    parent = _parent_and_child(ledger=ledger)

    result = parent.invoke({"messages": [HumanMessage(content="go")]}, config={"configurable": {"thread_id": "t"}})

    assert "The sum is 5." in _tool_messages(result)[0]
    assert ledger.subagent_spawns == 1


def test_no_ledger_keeps_the_old_behaviour() -> None:
    parent = _parent_and_child(ledger=None)
    result = parent.invoke({"messages": [HumanMessage(content="go")]}, config={"configurable": {"thread_id": "t"}})
    assert "The sum is 5." in _tool_messages(result)[0]


def test_cost_cap_is_checked_before_counting_a_spawn() -> None:
    class _OverBudget(CostLedger):
        def check_cost(self) -> CapDecision:
            return CapDecision(False, "cost cap reached ($1.0000 of $0.50)")

    ledger = _OverBudget(caps=RunawayCaps(max_subagents=5))
    refusal = _cap_refusal(ledger, "general-purpose")
    assert refusal is not None and "cost cap reached" in refusal
    assert ledger.subagent_spawns == 0  # a refused spawn is not counted


def test_no_ledger_never_refuses() -> None:
    assert _cap_refusal(None, "general-purpose") is None
