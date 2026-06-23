"""Hardening tests for the worktree middleware (S16, S21).

S16 — `create_worktree` must raise RuntimeError when git fails instead of
returning a WorktreeInfo for a worktree that was never created.

S21 — `spawn_parallel_tasks` must expose an async coroutine so it works on
the `ainvoke` agent path (where the sync body would call
`asyncio.ensure_future` in a loop-less worker thread and crash); the sync
fallback must degrade gracefully when there is no running event loop.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore

from bog_agents.middleware import worktree as wt_mod
from bog_agents.middleware.worktree import (
    ParallelWorktreeMiddleware,
    WorktreeInfo,
    create_worktree,
)


def _make_runtime(tool_call_id: str = "call_worktree") -> ToolRuntime:
    return ToolRuntime(
        state={"messages": []},
        context=None,
        tool_call_id=tool_call_id,
        store=InMemoryStore(),
        stream_writer=lambda _: None,
        config={},
    )


class TestCreateWorktreeFailureSurfaces:
    """S16: git failures must raise, not return a bogus WorktreeInfo."""

    def test_raises_on_git_exit_code(self, tmp_path, monkeypatch):
        # Both the `-b` attempt and the retry report a non-zero exit code.
        monkeypatch.setattr(wt_mod, "_run_git", lambda *a, **k: "[exit code 128]\nfatal: branch exists")

        with pytest.raises(RuntimeError, match="failed to create worktree for feature-x"):
            create_worktree(tmp_path, "feature-x", base_dir=tmp_path)

    def test_raises_on_git_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wt_mod, "_run_git", lambda *a, **k: "Error: git is not installed or not in PATH")

        with pytest.raises(RuntimeError, match="failed to create worktree"):
            create_worktree(tmp_path, "feature-y", base_dir=tmp_path)

    def test_raises_when_dir_not_created(self, tmp_path, monkeypatch):
        # git reports success (empty output) but never created the directory.
        monkeypatch.setattr(wt_mod, "_run_git", lambda *a, **k: "")

        with pytest.raises(RuntimeError, match="failed to create worktree"):
            create_worktree(tmp_path, "ghost", base_dir=tmp_path)

    def test_success_returns_worktree_info(self, tmp_path, monkeypatch):
        calls: list[tuple[str, ...]] = []

        def fake_run_git(_repo: object, *args: str, **_kwargs: object) -> str:
            calls.append(args)
            if args[:2] == ("worktree", "add"):
                # Simulate git creating the worktree directory.
                (tmp_path / "good").mkdir(exist_ok=True)
                return ""
            if args[:2] == ("rev-parse", "HEAD"):
                return "abc1234"
            return ""

        monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)

        info = create_worktree(tmp_path, "good", base_dir=tmp_path)
        assert isinstance(info, WorktreeInfo)
        assert info.branch == "good"
        assert info.commit == "abc1234"


class TestSpawnParallelTasksAsync:
    """S21: the parallel-spawn tool must work on the async agent path."""

    def _get_tool(self, tmp_path):
        mw = ParallelWorktreeMiddleware(working_dir=tmp_path, agent_factory=None)
        tool = next(t for t in mw.tools if t.name == "spawn_parallel_tasks")
        return mw, tool

    def test_tool_has_coroutine_registered(self, tmp_path):
        _mw, tool = self._get_tool(tmp_path)
        # The async path must have a coroutine; otherwise LangChain runs the
        # sync func in a worker thread with no loop and ensure_future crashes.
        assert tool.coroutine is not None

    async def test_async_invoke_registers_tasks(self, tmp_path, monkeypatch):
        mw, tool = self._get_tool(tmp_path)

        # Don't actually create worktrees / run agents.
        async def noop(_task):
            return None

        monkeypatch.setattr(mw, "_run_task_in_worktree", noop)

        spec = json.dumps([{"label": "auth", "prompt": "do auth"}, {"label": "db", "prompt": "do db"}])
        result = await tool.coroutine(_make_runtime(), spec)

        assert "Spawned 2 parallel task(s)" in result
        assert len(mw.get_tasks()) == 2
        # Let the scheduled background gather run to completion.
        await asyncio.gather(*list(mw._background_tasks), return_exceptions=True)

    async def test_async_invoke_invalid_json(self, tmp_path):
        _mw, tool = self._get_tool(tmp_path)
        result = await tool.coroutine(_make_runtime(), "{not json")
        assert result.startswith("Invalid JSON")

    def test_sync_fallback_degrades_without_loop(self, tmp_path):
        # Calling the raw sync func with no running event loop must not raise.
        mw = ParallelWorktreeMiddleware(working_dir=tmp_path, agent_factory=None)
        tool = next(t for t in mw.tools if t.name == "spawn_parallel_tasks")

        spec = json.dumps([{"label": "auth", "prompt": "do auth"}])
        # tool.func is the sync callable; invoke it outside any event loop.
        result = tool.func(_make_runtime(), spec)

        # Tasks are still registered and visible, even if not started.
        assert len(mw.get_tasks()) == 1
        assert "task(s)" in result
