"""Tests for async subagent middleware."""

from __future__ import annotations

import sys
import types

from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from bog_agents.middleware.async_subagents import AsyncSubAgentMiddleware


def _make_runtime(tool_call_id: str = "call_async") -> ToolRuntime:
    return ToolRuntime(
        state={"messages": [], "async_tasks": {}},
        context=None,
        tool_call_id=tool_call_id,
        store=InMemoryStore(),
        stream_writer=lambda _: None,
        config={},
    )


def _apply_command_update(runtime: ToolRuntime, command: Command) -> None:
    update = command.update or {}
    for key, value in update.items():
        if key == "async_tasks":
            runtime.state.setdefault("async_tasks", {}).update(value)
        elif key == "messages":
            runtime.state.setdefault("messages", []).extend(value)
        else:
            runtime.state[key] = value


def _install_fake_langgraph_sdk(monkeypatch) -> dict[str, object]:
    remote_state: dict[str, object] = {"threads": {}, "next_thread": 1, "next_run": 1}

    class FakeThreads:
        def create(self):
            thread_id = f"thread-{remote_state['next_thread']}"
            remote_state["next_thread"] += 1
            remote_state["threads"][thread_id] = {"values": {"messages": []}, "runs": {}}
            return {"thread_id": thread_id}

        async def acreate(self):
            return self.create()

        def get_state(self, thread_id: str):
            return remote_state["threads"][thread_id]

        async def aget_state(self, thread_id: str):
            return self.get_state(thread_id)

    class FakeRuns:
        def create(self, *args, **kwargs):
            thread_id = kwargs.get("thread_id") or args[0]
            graph_id = kwargs.get("assistant_id") or args[1]
            input_payload = kwargs.get("input")
            description = input_payload["messages"][0]["content"]
            run_id = f"run-{remote_state['next_run']}"
            remote_state["next_run"] += 1
            remote_state["threads"][thread_id]["runs"][run_id] = {
                "run_id": run_id,
                "status": "running",
                "error": None,
                "graph_id": graph_id,
                "description": description,
            }
            return {"run_id": run_id}

        async def acreate(self, *args, **kwargs):
            return self.create(*args, **kwargs)

        def get(self, thread_id: str, run_id: str):
            return remote_state["threads"][thread_id]["runs"][run_id]

        async def aget(self, thread_id: str, run_id: str):
            return self.get(thread_id, run_id)

        def cancel(self, thread_id: str, run_id: str):
            remote_state["threads"][thread_id]["runs"][run_id]["status"] = "cancelled"

        async def acancel(self, thread_id: str, run_id: str):
            self.cancel(thread_id, run_id)

    class FakeSyncClient:
        def __init__(self):
            self.threads = FakeThreads()
            self.runs = FakeRuns()

    class FakeAsyncClient:
        def __init__(self):
            self.threads = types.SimpleNamespace(
                create=FakeThreads().acreate,
                get_state=FakeThreads().aget_state,
            )
            self.runs = types.SimpleNamespace(
                create=FakeRuns().acreate,
                get=FakeRuns().aget,
                cancel=FakeRuns().acancel,
            )

    fake_sdk = types.ModuleType("langgraph_sdk")
    fake_sdk.get_sync_client = lambda **kwargs: FakeSyncClient()
    fake_sdk.get_client = lambda **kwargs: FakeAsyncClient()
    monkeypatch.setitem(sys.modules, "langgraph_sdk", fake_sdk)
    return remote_state


def test_async_subagent_tools_manage_remote_task_lifecycle(monkeypatch) -> None:
    """Start, check, update, cancel, and list tools should manage tracked tasks."""
    remote_state = _install_fake_langgraph_sdk(monkeypatch)
    middleware = AsyncSubAgentMiddleware(
        async_subagents=[
            {
                "name": "researcher",
                "description": "Remote research agent",
                "graph_id": "research-graph",
                "url": "https://example.test",
            }
        ]
    )
    tool_map = {tool.name: tool for tool in middleware.tools}
    runtime = _make_runtime()

    start_result = tool_map["start_async_task"].func(
        description="Research the latest release",
        subagent_type="researcher",
        runtime=runtime,
    )
    assert isinstance(start_result, Command)
    _apply_command_update(runtime, start_result)

    task_id = next(iter(runtime.state["async_tasks"]))
    task = runtime.state["async_tasks"][task_id]
    assert task["status"] == "running"

    thread = remote_state["threads"][task_id]
    run = thread["runs"][task["run_id"]]
    run["status"] = "success"
    thread["values"]["messages"] = [{"content": [{"type": "text", "text": "Remote result"}]}]

    check_result = tool_map["check_async_task"].func(task_id=task_id, runtime=runtime)
    assert isinstance(check_result, Command)
    _apply_command_update(runtime, check_result)
    assert runtime.state["async_tasks"][task_id]["status"] == "success"
    assert '"result": "Remote result"' in runtime.state["messages"][-1].content

    update_result = tool_map["update_async_task"].func(
        task_id=task_id,
        message="Please continue with a new angle.",
        runtime=runtime,
    )
    assert isinstance(update_result, Command)
    _apply_command_update(runtime, update_result)
    updated_task = runtime.state["async_tasks"][task_id]
    assert updated_task["status"] == "running"
    assert updated_task["run_id"] != task["run_id"]

    cancel_result = tool_map["cancel_async_task"].func(task_id=task_id, runtime=runtime)
    assert isinstance(cancel_result, Command)
    _apply_command_update(runtime, cancel_result)
    assert runtime.state["async_tasks"][task_id]["status"] == "cancelled"

    list_result = tool_map["list_async_tasks"].func(status_filter="all", runtime=runtime)
    assert isinstance(list_result, Command)
    assert '"task_id": "thread-1"' in list_result.update["messages"][0].content


async def test_async_subagent_async_tool_path(monkeypatch) -> None:
    """Async tool wrappers should use the async LangGraph SDK client path."""
    _install_fake_langgraph_sdk(monkeypatch)
    middleware = AsyncSubAgentMiddleware(
        async_subagents=[
            {
                "name": "builder",
                "description": "Remote builder agent",
                "graph_id": "builder-graph",
                "url": "https://example.test",
            }
        ]
    )
    runtime = _make_runtime("call_async_start")

    result = await middleware.tools[0].coroutine(
        description="Build a report",
        subagent_type="builder",
        runtime=runtime,
    )
    assert isinstance(result, Command)
    assert "async_tasks" in result.update
