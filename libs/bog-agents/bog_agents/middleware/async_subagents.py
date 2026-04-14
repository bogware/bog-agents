"""Middleware for async subagents running on remote LangGraph servers."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from bog_agents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


class AsyncSubAgent(TypedDict):
    """Specification for an async subagent running on a remote LangGraph server."""

    name: str
    description: str
    graph_id: str
    url: NotRequired[str]
    headers: NotRequired[dict[str, str]]


class AsyncTask(TypedDict):
    """Tracked async subagent task state."""

    task_id: str
    agent_name: str
    thread_id: str
    run_id: str
    status: str
    created_at: str
    last_checked_at: str
    last_updated_at: str


def _tasks_reducer(
    existing: dict[str, AsyncTask] | None,
    update: dict[str, AsyncTask],
) -> dict[str, AsyncTask]:
    """Merge async task updates into state."""
    merged = dict(existing or {})
    merged.update(update)
    return merged


class AsyncSubAgentState(AgentState):
    """State extension for async subagent tracking."""

    async_tasks: Annotated[NotRequired[dict[str, AsyncTask]], _tasks_reducer]


class StartAsyncTaskSchema(BaseModel):
    """Input schema for `start_async_task`."""

    description: str = Field(description="A detailed description of the task for the async subagent to perform.")
    subagent_type: str = Field(description="The type of async subagent to use. Must be one of the available agent types.")


class CheckAsyncTaskSchema(BaseModel):
    """Input schema for `check_async_task`."""

    task_id: str = Field(description="The exact task ID returned by start_async_task.")


class UpdateAsyncTaskSchema(BaseModel):
    """Input schema for `update_async_task`."""

    task_id: str = Field(description="The exact task ID returned by start_async_task.")
    message: str = Field(description="Follow-up instructions or context to send to the running async task.")


class CancelAsyncTaskSchema(BaseModel):
    """Input schema for `cancel_async_task`."""

    task_id: str = Field(description="The exact task ID returned by start_async_task.")


class ListAsyncTasksSchema(BaseModel):
    """Input schema for `list_async_tasks`."""

    status_filter: Literal["running", "success", "error", "cancelled", "all"] | None = Field(
        default=None,
        description="Optional filter for task status. Use 'all' to return every tracked task.",
    )


ASYNC_TASK_TOOL_DESCRIPTION = """Start an async subagent on a remote server. The subagent runs in the background and returns a task ID immediately.

Available async agent types:
{available_agents}

Usage notes:
1. This tool launches a background task and returns immediately with a task ID.
2. Use `check_async_task` when the user asks for a status update or result.
3. Use `update_async_task` to send new instructions to a running task.
4. Use `cancel_async_task` to stop a task that is no longer needed.
5. Use `list_async_tasks` to refresh the current state of every tracked task."""


ASYNC_TASK_SYSTEM_PROMPT = """## Async Subagents

You have access to async subagent tools that launch background tasks on remote LangGraph servers.

Tools:
- `start_async_task`: start a new background task and get a task ID immediately
- `check_async_task`: fetch the current status of a specific task
- `update_async_task`: send a follow-up instruction to a running task
- `cancel_async_task`: stop a running task
- `list_async_tasks`: refresh the current status of tracked tasks

Rules:
- After launching a task, return control to the user instead of polling immediately.
- Never report stale task status from memory. Use a tool to refresh it first.
- Use `list_async_tasks` when the user asks about all tasks and `check_async_task` for one task.
- Always show the full task ID."""


def _utc_now() -> str:
    """Return the current UTC time in compact ISO-8601 format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_headers(spec: AsyncSubAgent) -> dict[str, str]:
    """Build headers for a remote LangGraph server."""
    headers = dict(spec.get("headers") or {})
    if "x-auth-scheme" not in headers:
        headers["x-auth-scheme"] = "langsmith"
    return headers


def _load_sdk() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Load LangGraph SDK client factories lazily."""
    from langgraph_sdk import get_client, get_sync_client

    return get_client, get_sync_client


class _ClientCache:
    """Lazily-created clients keyed by async-subagent endpoint config."""

    def __init__(self, agents: dict[str, AsyncSubAgent]) -> None:
        self._agents = agents
        self._sync: dict[tuple[str | None, frozenset[tuple[str, str]]], Any] = {}
        self._async: dict[tuple[str | None, frozenset[tuple[str, str]]], Any] = {}

    def _cache_key(self, spec: AsyncSubAgent) -> tuple[str | None, frozenset[tuple[str, str]]]:
        return (spec.get("url"), frozenset(_resolve_headers(spec).items()))

    def get_sync(self, name: str) -> Any:
        """Get or create a sync client for the named async subagent."""
        spec = self._agents[name]
        if spec.get("url") is None:
            msg = f"Async subagent '{name}' has no url configured. Sync launch requires a concrete url."
            raise ValueError(msg)
        key = self._cache_key(spec)
        if key not in self._sync:
            _get_client, get_sync_client = _load_sdk()
            self._sync[key] = get_sync_client(url=spec.get("url"), headers=_resolve_headers(spec))
        return self._sync[key]

    def get_async(self, name: str) -> Any:
        """Get or create an async client for the named async subagent."""
        spec = self._agents[name]
        key = self._cache_key(spec)
        if key not in self._async:
            get_client, _get_sync_client = _load_sdk()
            self._async[key] = get_client(url=spec.get("url"), headers=_resolve_headers(spec))
        return self._async[key]


def _validate_agent_type(agent_map: dict[str, AsyncSubAgent], agent_type: str) -> str | None:
    """Validate that the requested async subagent type exists."""
    if agent_type not in agent_map:
        allowed = ", ".join(f"`{name}`" for name in agent_map)
        return f"Unknown async subagent type `{agent_type}`. Available types: {allowed}"
    return None


def _coerce_result_text(value: Any) -> str:
    """Convert remote result payloads into a compact text representation."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts = [block.get("text", "") for block in value if isinstance(block, dict) and block.get("type") == "text"]
        if text_parts:
            return "\n".join(part for part in text_parts if part)
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


def _extract_run_result(thread_values: dict[str, Any], status: str, run_error: Any) -> dict[str, Any]:
    """Build a serializable task status payload."""
    result: dict[str, Any] = {"status": status}
    if status == "success":
        messages = thread_values.get("messages", [])
        if messages:
            last = messages[-1]
            if isinstance(last, dict):
                result["result"] = _coerce_result_text(last.get("content", ""))
            else:
                result["result"] = str(last)
        else:
            result["result"] = "(completed with no output messages)"
    elif status == "error":
        result["error"] = str(run_error) if run_error else "The async subagent encountered an error."
    return result


def _build_task_update(task: AsyncTask, *, status: str, run_id: str | None = None, status_changed: bool = False) -> AsyncTask:
    """Create an updated AsyncTask record."""
    now = _utc_now()
    return AsyncTask(
        task_id=task["task_id"],
        agent_name=task["agent_name"],
        thread_id=task["thread_id"],
        run_id=run_id or task["run_id"],
        status=status,
        created_at=task["created_at"],
        last_checked_at=now,
        last_updated_at=now if status_changed or run_id is not None else task["last_updated_at"],
    )


def _tracked_task(runtime: ToolRuntime, task_id: str) -> AsyncTask | str:
    """Resolve a tracked async task from runtime state."""
    tasks = runtime.state.get("async_tasks", {})
    task = tasks.get(task_id)
    if task is None:
        return f"Unknown async task `{task_id}`. Use list_async_tasks to see tracked task IDs."
    return task


def _create_run_sync(client: Any, *, thread_id: str, graph_id: str, description: str) -> Any:
    """Create a remote run with compatibility for sdk call signatures."""
    try:
        return client.runs.create(
            thread_id=thread_id,
            assistant_id=graph_id,
            input={"messages": [{"role": "user", "content": description}]},
        )
    except TypeError:
        return client.runs.create(thread_id, graph_id, input={"messages": [{"role": "user", "content": description}]})


async def _acreate_run(client: Any, *, thread_id: str, graph_id: str, description: str) -> Any:
    """Async version of run creation with compatibility fallbacks."""
    try:
        return await client.runs.create(
            thread_id=thread_id,
            assistant_id=graph_id,
            input={"messages": [{"role": "user", "content": description}]},
        )
    except TypeError:
        return await client.runs.create(thread_id, graph_id, input={"messages": [{"role": "user", "content": description}]})


def _task_summary(task: AsyncTask) -> dict[str, str]:
    """Build a compact serializable summary for listing."""
    return {
        "task_id": task["task_id"],
        "agent_name": task["agent_name"],
        "status": task["status"],
        "created_at": task["created_at"],
        "last_checked_at": task["last_checked_at"],
        "last_updated_at": task["last_updated_at"],
    }


class AsyncSubAgentMiddleware(AgentMiddleware[AsyncSubAgentState, ContextT, ResponseT]):
    """Middleware that adds remote async-subagent management tools."""

    def __init__(
        self,
        *,
        async_subagents: list[AsyncSubAgent],
        system_prompt: str | None = ASYNC_TASK_SYSTEM_PROMPT,
        task_description: str | None = None,
    ) -> None:
        """Initialize async-subagent middleware."""
        super().__init__()
        self._async_subagents = {spec["name"]: spec for spec in async_subagents}
        self._clients = _ClientCache(self._async_subagents)

        available_agents = "\n".join(f"- {spec['name']}: {spec['description']}" for spec in async_subagents)
        start_description = (task_description or ASYNC_TASK_TOOL_DESCRIPTION).format(available_agents=available_agents)
        start_tool = self._build_start_tool(start_description)
        check_tool = self._build_check_tool()
        update_tool = self._build_update_tool()
        cancel_tool = self._build_cancel_tool()
        self.tools = [
            start_tool,
            check_tool,
            update_tool,
            cancel_tool,
            self._build_list_tool(check_tool),
        ]
        self.system_prompt = system_prompt

    def _build_start_tool(self, tool_description: str) -> StructuredTool:
        """Build the `start_async_task` tool."""

        def start_async_task(
            description: str,
            subagent_type: str,
            runtime: ToolRuntime,
        ) -> str | Command:
            error = _validate_agent_type(self._async_subagents, subagent_type)
            if error is not None:
                return error

            spec = self._async_subagents[subagent_type]
            try:
                client = self._clients.get_sync(subagent_type)
                thread = client.threads.create()
                run = _create_run_sync(client, thread_id=thread["thread_id"], graph_id=spec["graph_id"], description=description)
            except Exception as exc:
                logger.warning("Failed to launch async subagent '%s': %s", subagent_type, exc)
                return f"Failed to launch async subagent '{subagent_type}': {exc}"

            now = _utc_now()
            task_id = thread["thread_id"]
            task = AsyncTask(
                task_id=task_id,
                agent_name=subagent_type,
                thread_id=task_id,
                run_id=run["run_id"],
                status="running",
                created_at=now,
                last_checked_at=now,
                last_updated_at=now,
            )
            message = f"Launched async subagent.\ntask_id: {task_id}"
            return Command(update={"messages": [ToolMessage(message, tool_call_id=runtime.tool_call_id)], "async_tasks": {task_id: task}})

        async def astart_async_task(
            description: str,
            subagent_type: str,
            runtime: ToolRuntime,
        ) -> str | Command:
            error = _validate_agent_type(self._async_subagents, subagent_type)
            if error is not None:
                return error

            spec = self._async_subagents[subagent_type]
            try:
                client = self._clients.get_async(subagent_type)
                thread = await client.threads.create()
                run = await _acreate_run(client, thread_id=thread["thread_id"], graph_id=spec["graph_id"], description=description)
            except Exception as exc:
                logger.warning("Failed to launch async subagent '%s': %s", subagent_type, exc)
                return f"Failed to launch async subagent '{subagent_type}': {exc}"

            now = _utc_now()
            task_id = thread["thread_id"]
            task = AsyncTask(
                task_id=task_id,
                agent_name=subagent_type,
                thread_id=task_id,
                run_id=run["run_id"],
                status="running",
                created_at=now,
                last_checked_at=now,
                last_updated_at=now,
            )
            message = f"Launched async subagent.\ntask_id: {task_id}"
            return Command(update={"messages": [ToolMessage(message, tool_call_id=runtime.tool_call_id)], "async_tasks": {task_id: task}})

        return StructuredTool.from_function(
            name="start_async_task",
            func=start_async_task,
            coroutine=astart_async_task,
            description=tool_description,
            infer_schema=False,
            args_schema=StartAsyncTaskSchema,
        )

    def _build_check_tool(self) -> StructuredTool:
        """Build the `check_async_task` tool."""

        def check_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
            tracked = _tracked_task(runtime, task_id)
            if isinstance(tracked, str):
                return tracked

            try:
                client = self._clients.get_sync(tracked["agent_name"])
                run = client.runs.get(tracked["thread_id"], tracked["run_id"])
                state = client.threads.get_state(tracked["thread_id"])
            except Exception as exc:
                logger.warning("Failed to check async task '%s': %s", task_id, exc)
                return f"Failed to check async task '{task_id}': {exc}"

            values = state.get("values", {}) if isinstance(state, dict) else {}
            result = _extract_run_result(values, run.get("status", tracked["status"]), run.get("error"))
            updated_task = _build_task_update(
                tracked,
                status=result["status"],
                status_changed=result["status"] != tracked["status"],
            )
            return Command(
                update={
                    "messages": [ToolMessage(json.dumps({"task_id": task_id, **result}), tool_call_id=runtime.tool_call_id)],
                    "async_tasks": {task_id: updated_task},
                }
            )

        async def acheck_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
            tracked = _tracked_task(runtime, task_id)
            if isinstance(tracked, str):
                return tracked

            try:
                client = self._clients.get_async(tracked["agent_name"])
                run = await client.runs.get(tracked["thread_id"], tracked["run_id"])
                state = await client.threads.get_state(tracked["thread_id"])
            except Exception as exc:
                logger.warning("Failed to check async task '%s': %s", task_id, exc)
                return f"Failed to check async task '{task_id}': {exc}"

            values = state.get("values", {}) if isinstance(state, dict) else {}
            result = _extract_run_result(values, run.get("status", tracked["status"]), run.get("error"))
            updated_task = _build_task_update(
                tracked,
                status=result["status"],
                status_changed=result["status"] != tracked["status"],
            )
            return Command(
                update={
                    "messages": [ToolMessage(json.dumps({"task_id": task_id, **result}), tool_call_id=runtime.tool_call_id)],
                    "async_tasks": {task_id: updated_task},
                }
            )

        return StructuredTool.from_function(
            name="check_async_task",
            func=check_async_task,
            coroutine=acheck_async_task,
            description="Get the current status of a tracked async task and include the final result when it has completed.",
            infer_schema=False,
            args_schema=CheckAsyncTaskSchema,
        )

    def _build_update_tool(self) -> StructuredTool:
        """Build the `update_async_task` tool."""

        def update_async_task(task_id: str, message: str, runtime: ToolRuntime) -> str | Command:
            tracked = _tracked_task(runtime, task_id)
            if isinstance(tracked, str):
                return tracked

            spec = self._async_subagents[tracked["agent_name"]]
            try:
                client = self._clients.get_sync(tracked["agent_name"])
                run = _create_run_sync(client, thread_id=tracked["thread_id"], graph_id=spec["graph_id"], description=message)
            except Exception as exc:
                logger.warning("Failed to update async task '%s': %s", task_id, exc)
                return f"Failed to update async task '{task_id}': {exc}"

            updated_task = _build_task_update(tracked, status="running", run_id=run["run_id"], status_changed=tracked["status"] != "running")
            response = json.dumps({"task_id": task_id, "status": "running", "message": "Sent follow-up instructions to async task."})
            return Command(
                update={
                    "messages": [ToolMessage(response, tool_call_id=runtime.tool_call_id)],
                    "async_tasks": {task_id: updated_task},
                }
            )

        async def aupdate_async_task(task_id: str, message: str, runtime: ToolRuntime) -> str | Command:
            tracked = _tracked_task(runtime, task_id)
            if isinstance(tracked, str):
                return tracked

            spec = self._async_subagents[tracked["agent_name"]]
            try:
                client = self._clients.get_async(tracked["agent_name"])
                run = await _acreate_run(client, thread_id=tracked["thread_id"], graph_id=spec["graph_id"], description=message)
            except Exception as exc:
                logger.warning("Failed to update async task '%s': %s", task_id, exc)
                return f"Failed to update async task '{task_id}': {exc}"

            updated_task = _build_task_update(tracked, status="running", run_id=run["run_id"], status_changed=tracked["status"] != "running")
            response = json.dumps({"task_id": task_id, "status": "running", "message": "Sent follow-up instructions to async task."})
            return Command(
                update={
                    "messages": [ToolMessage(response, tool_call_id=runtime.tool_call_id)],
                    "async_tasks": {task_id: updated_task},
                }
            )

        return StructuredTool.from_function(
            name="update_async_task",
            func=update_async_task,
            coroutine=aupdate_async_task,
            description="Send new instructions to a running async task and continue on the same tracked thread.",
            infer_schema=False,
            args_schema=UpdateAsyncTaskSchema,
        )

    def _build_cancel_tool(self) -> StructuredTool:
        """Build the `cancel_async_task` tool."""

        def cancel_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
            tracked = _tracked_task(runtime, task_id)
            if isinstance(tracked, str):
                return tracked

            try:
                client = self._clients.get_sync(tracked["agent_name"])
                cancel = getattr(client.runs, "cancel", None)
                if cancel is None:
                    return f"Async task cancellation is not supported for `{tracked['agent_name']}`."
                cancel(tracked["thread_id"], tracked["run_id"])
            except Exception as exc:
                logger.warning("Failed to cancel async task '%s': %s", task_id, exc)
                return f"Failed to cancel async task '{task_id}': {exc}"

            updated_task = _build_task_update(tracked, status="cancelled", status_changed=tracked["status"] != "cancelled")
            response = json.dumps({"task_id": task_id, "status": "cancelled"})
            return Command(
                update={
                    "messages": [ToolMessage(response, tool_call_id=runtime.tool_call_id)],
                    "async_tasks": {task_id: updated_task},
                }
            )

        async def acancel_async_task(task_id: str, runtime: ToolRuntime) -> str | Command:
            tracked = _tracked_task(runtime, task_id)
            if isinstance(tracked, str):
                return tracked

            try:
                client = self._clients.get_async(tracked["agent_name"])
                cancel = getattr(client.runs, "cancel", None)
                if cancel is None:
                    return f"Async task cancellation is not supported for `{tracked['agent_name']}`."
                await cancel(tracked["thread_id"], tracked["run_id"])
            except Exception as exc:
                logger.warning("Failed to cancel async task '%s': %s", task_id, exc)
                return f"Failed to cancel async task '{task_id}': {exc}"

            updated_task = _build_task_update(tracked, status="cancelled", status_changed=tracked["status"] != "cancelled")
            response = json.dumps({"task_id": task_id, "status": "cancelled"})
            return Command(
                update={
                    "messages": [ToolMessage(response, tool_call_id=runtime.tool_call_id)],
                    "async_tasks": {task_id: updated_task},
                }
            )

        return StructuredTool.from_function(
            name="cancel_async_task",
            func=cancel_async_task,
            coroutine=acancel_async_task,
            description="Cancel a tracked async task.",
            infer_schema=False,
            args_schema=CancelAsyncTaskSchema,
        )

    def _build_list_tool(self, check_tool: StructuredTool) -> StructuredTool:
        """Build the `list_async_tasks` tool."""

        def list_async_tasks(status_filter: Literal["running", "success", "error", "cancelled", "all"] | None, runtime: ToolRuntime) -> str | Command:
            requested_status = status_filter or "all"
            tasks = runtime.state.get("async_tasks", {})
            refreshed_tasks: dict[str, AsyncTask] = {}
            visible_tasks: list[dict[str, str]] = []
            check_func = check_tool.func
            if check_func is None:
                return "Async task refresh is unavailable because the check tool has no synchronous implementation."

            for task_id, tracked in tasks.items():
                updated = tracked
                if tracked["status"] not in {"success", "error", "cancelled"}:
                    result = check_func(task_id=task_id, runtime=runtime)
                    if isinstance(result, Command):
                        updated = result.update["async_tasks"][task_id]
                refreshed_tasks[task_id] = updated
                if requested_status == "all" or updated["status"] == requested_status:
                    visible_tasks.append(_task_summary(updated))

            return Command(
                update={
                    "messages": [ToolMessage(json.dumps(visible_tasks), tool_call_id=runtime.tool_call_id)],
                    "async_tasks": refreshed_tasks,
                }
            )

        async def alist_async_tasks(
            status_filter: Literal["running", "success", "error", "cancelled", "all"] | None,
            runtime: ToolRuntime,
        ) -> str | Command:
            requested_status = status_filter or "all"
            tasks = runtime.state.get("async_tasks", {})
            refreshed_tasks: dict[str, AsyncTask] = {}
            visible_tasks: list[dict[str, str]] = []
            check_coroutine = check_tool.coroutine
            if check_coroutine is None:
                return "Async task refresh is unavailable because the check tool has no async implementation."

            for task_id, tracked in tasks.items():
                updated = tracked
                if tracked["status"] not in {"success", "error", "cancelled"}:
                    result = await check_coroutine(
                        task_id=task_id,
                        runtime=runtime,
                    )
                    if isinstance(result, Command):
                        updated = result.update["async_tasks"][task_id]
                refreshed_tasks[task_id] = updated
                if requested_status == "all" or updated["status"] == requested_status:
                    visible_tasks.append(_task_summary(updated))

            return Command(
                update={
                    "messages": [ToolMessage(json.dumps(visible_tasks), tool_call_id=runtime.tool_call_id)],
                    "async_tasks": refreshed_tasks,
                }
            )

        return StructuredTool.from_function(
            name="list_async_tasks",
            func=list_async_tasks,
            coroutine=alist_async_tasks,
            description="List tracked async tasks and refresh live statuses for any tasks still in progress.",
            infer_schema=False,
            args_schema=ListAsyncTasksSchema,
        )

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Append async-subagent instructions to the system prompt."""
        if self.system_prompt is not None:
            system_message = append_to_system_message(request.system_message, self.system_prompt)
            return handler(request.override(system_message=system_message))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """Async version of system-prompt injection."""
        if self.system_prompt is not None:
            system_message = append_to_system_message(request.system_message, self.system_prompt)
            return await handler(request.override(system_message=system_message))
        return await handler(request)
