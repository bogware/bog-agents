"""Hardening tests for `bog_agents.middleware.checkpointing` (finding S1).

Regression coverage for the bug where `wrap_tool_call` / `awrap_tool_call`
read a non-existent `request.tool_calls` (plural) attribute. langgraph's
`ToolCallRequest` exposes a single `tool_call` dict, so the old `hasattr`
guard was always False and `_create_checkpoint` was never reached — leaving
`/undo` and `/rewind` with nothing to revert to.
"""

from __future__ import annotations

import subprocess

import pytest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from bog_agents.middleware.checkpointing import CheckpointingMiddleware


def _make_request(tool_name: str, tool_call_id: str = "call_1") -> ToolCallRequest:
    """Build a real `ToolCallRequest` carrying just the tool_call payload.

    Args:
        tool_name: Name of the tool being called.
        tool_call_id: Identifier for the tool call.

    Returns:
        A `ToolCallRequest` instance.
    """
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {}, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state=None,
        runtime=None,  # type: ignore[arg-type]
    )


def _git_available() -> bool:
    """Return True if a `git` executable is on PATH."""
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return True


requires_git = pytest.mark.skipif(not _git_available(), reason="git executable not available")


@requires_git
def test_wrap_tool_call_records_checkpoint_for_mutating_tool(tmp_path):
    """A mutating tool call drives `_create_checkpoint`, recording a checkpoint."""
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    handled: list[ToolCallRequest] = []

    def handler(request: ToolCallRequest) -> ToolMessage:
        handled.append(request)
        return ToolMessage(content="ok", tool_call_id="call_1")

    request = _make_request("write_file")
    result = middleware.wrap_tool_call(request, handler)

    # Handler must still be invoked exactly once with the same request.
    assert handled == [request]
    assert isinstance(result, ToolMessage)
    # At least one checkpoint must now exist tied to the mutating tool call.
    assert any(cp.tool_call_id == "call_1" for cp in middleware._checkpoints)


@requires_git
def test_wrap_tool_call_skips_checkpoint_for_non_mutating_tool(tmp_path):
    """A read-only tool call must not record a checkpoint."""
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id="call_1")

    middleware.wrap_tool_call(_make_request("read_file"), handler)

    assert middleware._checkpoints == []


@requires_git
async def test_awrap_tool_call_records_checkpoint_for_mutating_tool(tmp_path):
    """Async path also records a checkpoint for mutating tools."""
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    handled: list[ToolCallRequest] = []

    async def handler(request: ToolCallRequest) -> ToolMessage:
        handled.append(request)
        return ToolMessage(content="ok", tool_call_id="call_1")

    request = _make_request("edit_file", tool_call_id="call_2")
    result = await middleware.awrap_tool_call(request, handler)

    assert handled == [request]
    assert isinstance(result, ToolMessage)
    assert any(cp.tool_call_id == "call_2" for cp in middleware._checkpoints)


def test_wrap_tool_call_invokes_create_checkpoint(tmp_path, monkeypatch):
    """`_create_checkpoint` is reached for mutating tools regardless of git state.

    This isolates the dispatch logic from git by stubbing `_create_checkpoint`,
    guarding the exact regression: the old `request.tool_calls` lookup never
    fired, so the stub would never be called.
    """
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        middleware,
        "_create_checkpoint",
        lambda name, tcid: calls.append((name, tcid)) or "deadbeef",
    )

    def handler(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content="ok", tool_call_id="call_1")

    middleware.wrap_tool_call(_make_request("multi_edit_file", "call_9"), handler)

    assert calls == [("multi_edit_file", "call_9")]


def test_run_git_missing_binary_self_disables(tmp_path, monkeypatch):
    """CTX-1: a missing `git` binary returns a synthetic failure and disables."""
    import bog_agents.middleware.checkpointing as ckpt

    def boom(*_a: object, **_k: object):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(ckpt.subprocess, "run", boom)
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    result = middleware._run_git("status")

    assert result.returncode == 1
    assert result.stdout == ""
    assert middleware._enabled is False


def test_run_git_timeout_self_disables(tmp_path, monkeypatch):
    """CTX-1: a git call that times out returns a synthetic failure and disables."""
    import bog_agents.middleware.checkpointing as ckpt

    def boom(*_a: object, **_k: object):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(ckpt.subprocess, "run", boom)
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    result = middleware._run_git("add", "-A")

    assert result.returncode == 1
    assert middleware._enabled is False


def test_wrap_tool_call_survives_missing_git(tmp_path, monkeypatch):
    """CTX-1: a mutating tool call must not crash when git is unavailable.

    The CLI ships checkpointing on by default, so on a box without git every
    write_file/edit_file/execute reached _run_git via _create_checkpoint. The
    uncaught FileNotFoundError previously propagated out of the tool node (which
    langgraph re-raises) and killed the whole turn.
    """
    import bog_agents.middleware.checkpointing as ckpt

    def boom(*_a: object, **_k: object):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(ckpt.subprocess, "run", boom)
    middleware = CheckpointingMiddleware(working_dir=tmp_path)

    handled: list[ToolCallRequest] = []

    def handler(request: ToolCallRequest) -> ToolMessage:
        handled.append(request)
        return ToolMessage(content="ok", tool_call_id="call_1")

    # Must not raise; the downstream tool still runs and gets its result.
    result = middleware.wrap_tool_call(_make_request("write_file"), handler)

    assert isinstance(result, ToolMessage)
    assert handled  # downstream handler was invoked despite git being absent
    assert middleware._enabled is False
