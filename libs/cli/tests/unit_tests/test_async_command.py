"""Tests for the /async slash command (Gap 4).

Exercises the dispatch + wait semantics in isolation by stubbing the
BackgroundAgentManager so no real agent runs. We don't spin up the
full Textual app — the handler logic that matters lives on
:class:`BogAgentsApp`, which we invoke via a minimal harness that
replaces the surface methods it touches.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from bog_agents_cli.background_agents import BackgroundStatus


@dataclass
class _FakeTask:
    task_id: str
    status: str = BackgroundStatus.RUNNING
    result: str = ""
    error: str = ""
    prompt: str = "do the thing"
    label: str = "test"


@dataclass
class _FakeManager:
    """Stand-in for BackgroundAgentManager."""

    tasks: dict[str, _FakeTask] = field(default_factory=dict)
    submitted: list[tuple[str, dict]] = field(default_factory=list)
    list_called: int = 0

    def add(self, task: _FakeTask) -> None:
        self.tasks[task.task_id] = task

    def get_status(self, task_id: str):
        return self.tasks.get(task_id)

    def format_status_table(self) -> str:
        self.list_called += 1
        return "TASKS-TABLE"

    def cancel(self, task_id: str) -> bool:
        t = self.tasks.get(task_id)
        if t is None:
            return False
        t.status = BackgroundStatus.CANCELLED
        return True


class _Harness:
    """Minimal stand-in for BogAgentsApp used by /async handlers.

    We import the actual handler bodies via ``app.BogAgentsApp`` and
    rebind ``self`` to this harness — every collaborator the
    handler touches (mount, manager, format helpers) is stubbed.
    """

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self._bg_manager = _FakeManager()

    async def _mount_message(self, msg: Any) -> None:  # noqa: ANN401
        self.messages.append(msg)

    async def _ensure_background_manager(self) -> None:
        # Already set in __init__.
        return

    async def _handle_background_command(self, command: str) -> None:
        # We don't go through the real handler; just record the
        # delegation. The /async handler is being tested for its own
        # routing logic.
        self.messages.append(("background-cmd", command))

    def _format_background_task_detail(self, task: _FakeTask) -> str:
        return f"DETAIL[{task.task_id}]={task.status}"

    @staticmethod
    def mounted_text(msg: Any) -> str:  # noqa: ANN401
        if isinstance(msg, tuple):
            return str(msg)
        return str(getattr(msg, "_text", msg))


def _text(msg) -> str:
    """Pull the readable text out of a UserMessage / AppMessage / tuple.

    The Textual widgets we mount don't have a stable str() but they
    do retain the original content on a private attribute. Best-effort:
    cycle through likely attrs, fall back to repr.
    """
    if isinstance(msg, tuple):
        return " ".join(str(part) for part in msg)
    for attr in ("_text", "text", "_content", "content", "renderable"):
        if hasattr(msg, attr):
            value = getattr(msg, attr)
            if value:
                return str(value)
    return repr(msg)


@pytest.fixture
def harness() -> _Harness:
    """Build a harness with the real ``_async_wait`` bound to it.

    ``_async_wait`` is an instance method on BogAgentsApp; we attach
    the unbound function so the harness behaves like an app for the
    handler-under-test without spinning up Textual.
    """
    from bog_agents_cli.app import BogAgentsApp

    h = _Harness()
    h._async_wait = BogAgentsApp._async_wait.__get__(h, _Harness)  # type: ignore[attr-defined]
    return h


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_with_no_args_lists(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async")  # type: ignore[arg-type]
    # First message is the UserMessage echo; second is the delegation.
    assert any(
        isinstance(m, tuple) and m[1] == "/background list"
        for m in harness.messages
    )


@pytest.mark.asyncio
async def test_async_list_alias(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async list")  # type: ignore[arg-type]
    assert any(
        isinstance(m, tuple) and m[1] == "/background list"
        for m in harness.messages
    )


@pytest.mark.asyncio
async def test_async_status_passes_through(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async status bg-001")  # type: ignore[arg-type]
    assert any(
        isinstance(m, tuple) and m[1] == "/background status bg-001"
        for m in harness.messages
    )


@pytest.mark.asyncio
async def test_async_cancel_passes_through(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async cancel bg-001")  # type: ignore[arg-type]
    assert any(
        isinstance(m, tuple) and m[1] == "/background cancel bg-001"
        for m in harness.messages
    )


@pytest.mark.asyncio
async def test_async_prompt_submits(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async refactor the cli")  # type: ignore[arg-type]
    assert any(
        isinstance(m, tuple) and m[1] == "/background refactor the cli"
        for m in harness.messages
    )


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_wait_missing_id(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async wait")  # type: ignore[arg-type]
    assert any(_text(m).startswith("Usage: /async wait") for m in harness.messages)


@pytest.mark.asyncio
async def test_async_wait_invalid_timeout(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(  # type: ignore[arg-type]
        harness, "/async wait bg-001 ten-seconds"
    )
    assert any("Invalid timeout" in _text(m) for m in harness.messages)


@pytest.mark.asyncio
async def test_async_wait_unknown_task(harness: _Harness):
    from bog_agents_cli.app import BogAgentsApp

    await BogAgentsApp._handle_async_command(harness, "/async wait bg-missing 5")  # type: ignore[arg-type]
    assert any("No background task" in _text(m) for m in harness.messages)


@pytest.mark.asyncio
async def test_async_wait_completes_with_detail(harness: _Harness, monkeypatch):
    from bog_agents_cli import app as app_module
    from bog_agents_cli.app import BogAgentsApp

    # Speed up the polling loop.
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(app_module.asyncio, "sleep", fast_sleep)

    task = _FakeTask(
        task_id="bg-001",
        status=BackgroundStatus.RUNNING,
        result="all done",
    )
    harness._bg_manager.add(task)

    # Flip to COMPLETED after the first poll.
    polls = {"n": 0}
    original_get = harness._bg_manager.get_status

    def stepped_get(task_id):
        polls["n"] += 1
        if polls["n"] >= 2:
            task.status = BackgroundStatus.COMPLETED
        return original_get(task_id)

    harness._bg_manager.get_status = stepped_get  # type: ignore[method-assign]

    await BogAgentsApp._handle_async_command(harness, "/async wait bg-001 30")  # type: ignore[arg-type]
    assert any("DETAIL[bg-001]" in _text(m) for m in harness.messages)


@pytest.mark.asyncio
async def test_async_wait_times_out(harness: _Harness, monkeypatch):
    from bog_agents_cli import app as app_module
    from bog_agents_cli.app import BogAgentsApp

    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(app_module.asyncio, "sleep", fast_sleep)
    # Patch the monotonic clock so the deadline expires after 1 tick.
    clock = [1000.0]

    def fake_monotonic():
        clock[0] += 5.0
        return clock[0]

    monkeypatch.setattr(app_module, "_monotonic", fake_monotonic)

    harness._bg_manager.add(
        _FakeTask(task_id="bg-002", status=BackgroundStatus.RUNNING)
    )

    await BogAgentsApp._handle_async_command(harness, "/async wait bg-002 1")  # type: ignore[arg-type]
    assert any("timed out" in _text(m) for m in harness.messages)
