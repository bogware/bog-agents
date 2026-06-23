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
    WorktreeTask,
    create_worktree,
    merge_with_conflict_report,
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


class TestCancelTaskNotResurrected:
    """P18: a cancelled task must not be overwritten to 'completed' in finally."""

    async def test_cancelled_task_stays_cancelled_after_worker_finishes(self, tmp_path, monkeypatch):
        # agent_factory that blocks until released so we can cancel mid-flight.
        release = asyncio.Event()

        def slow_factory(_prompt: str, _wd) -> str:
            # Simulate the (non-interruptible) to_thread worker still finishing.
            import time as _t

            while not release.is_set():
                _t.sleep(0.005)
            return "done"

        mw = ParallelWorktreeMiddleware(working_dir=tmp_path, agent_factory=slow_factory)

        created_wt = WorktreeInfo(path=tmp_path / "wt", branch="b")
        monkeypatch.setattr(wt_mod, "create_worktree", lambda *_a, **_k: created_wt)
        monkeypatch.setattr(wt_mod, "remove_worktree", lambda *_a, **_k: "")

        task = await mw._create_task(label="t", prompt="p")
        runner = asyncio.ensure_future(mw._run_task_in_worktree(task))

        # Wait until the task is actually running (handle registered).
        for _ in range(200):
            if task.status == "running":
                break
            await asyncio.sleep(0.005)
        assert task.status == "running"

        # Cancel via the tool while the to_thread worker is still busy.
        cancel_tool = next(t for t in mw.tools if t.name == "cancel_task")
        msg = cancel_tool.func(_make_runtime(), task.task_id)
        assert "cancelled" in msg
        assert task.status == "cancelled"

        # Let the (already-running) worker finish; it must NOT resurrect to completed.
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        assert task.status == "cancelled"
        assert task.result != "done" or task.status == "cancelled"

    def test_cancel_unknown_task(self, tmp_path):
        mw = ParallelWorktreeMiddleware(working_dir=tmp_path, agent_factory=None)
        cancel_tool = next(t for t in mw.tools if t.name == "cancel_task")
        assert "not found" in cancel_tool.func(_make_runtime(), "nope")

    async def test_cancel_already_completed_is_noop(self, tmp_path):
        mw = ParallelWorktreeMiddleware(working_dir=tmp_path, agent_factory=None)
        task = await mw._create_task(label="t", prompt="p")
        task.status = "completed"
        cancel_tool = next(t for t in mw.tools if t.name == "cancel_task")
        assert "already completed" in cancel_tool.func(_make_runtime(), task.task_id)


class TestTaskRepoRootNotRaced:
    """P19: per-task repo_root must isolate concurrent tasks from a shared attr."""

    async def test_create_task_does_not_mutate_shared_working_dir(self, tmp_path):
        mw = ParallelWorktreeMiddleware(working_dir=tmp_path / "default", agent_factory=None)
        other_root = tmp_path / "other"
        task = await mw._create_task(label="t", prompt="p", repo_root=other_root)

        # The task captures its own root...
        assert task.repo_root == other_root
        # ...and the shared instance attribute is untouched (no race surface).
        assert mw._working_dir == tmp_path / "default"

    async def test_run_uses_task_repo_root_not_shared(self, tmp_path, monkeypatch):
        mw = ParallelWorktreeMiddleware(working_dir=tmp_path / "default", agent_factory=None)

        seen_roots: list = []

        def fake_create(repo_root, _branch):
            seen_roots.append(repo_root)
            return WorktreeInfo(path=tmp_path / "wt", branch="b")

        monkeypatch.setattr(wt_mod, "create_worktree", fake_create)
        monkeypatch.setattr(wt_mod, "remove_worktree", lambda *_a, **_k: "")

        root_a = tmp_path / "repo-a"
        task = await mw._create_task(label="a", prompt="p", repo_root=root_a)

        # Simulate a concurrent _create_task repointing the shared dir BEFORE
        # the first task's coroutine runs — the old code would have raced here.
        await mw._create_task(label="b", prompt="p", repo_root=tmp_path / "repo-b")

        await mw._run_task_in_worktree(task)

        # create_worktree must have been called against the task's own root.
        assert seen_roots == [root_a]
        assert task.status == "completed"

    def test_default_repo_root_falls_back_to_working_dir(self, tmp_path):
        # Direct construction (agent-tool spawn path) with no repo_root still
        # resolves to the middleware working dir inside _run_task_in_worktree.
        task = WorktreeTask(label="x", prompt="p", branch="b")
        assert task.repo_root is None  # resolved at run time to self._working_dir


class TestMergeRestoresBranch:
    """P26: merge_with_conflict_report must restore the caller's branch on early-return."""

    def _git_recorder(self, *, merge_ok: bool, start_branch: str = "feature/work"):
        """Build a fake _run_git that tracks the current checked-out branch."""
        state = {"branch": start_branch}
        calls: list[tuple[str, ...]] = []

        def fake_run_git(_repo: object, *args: str, **_kwargs: object) -> str:
            calls.append(args)
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return state["branch"]
            if args[0] == "checkout":
                # args may be ("checkout", branch) or ("checkout", "--", branch)
                target = args[-1]
                state["branch"] = target
                return ""
            if args[0] == "merge":
                return "" if merge_ok else "[exit code 1]\nCONFLICT"
            return ""

        return fake_run_git, state, calls

    def test_restores_branch_on_manual_conflict_early_return(self, tmp_path, monkeypatch):
        fake_run_git, state, _calls = self._git_recorder(merge_ok=False)
        monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)
        monkeypatch.setattr(wt_mod, "detect_merge_conflicts", lambda *_a, **_k: ["src/a.py"])
        monkeypatch.setattr(wt_mod, "_is_trivial_conflict", lambda *_a, **_k: False)

        report = merge_with_conflict_report(tmp_path, "src", "main", strategy="manual")

        assert report["success"] is False
        assert report["conflicts"] == ["src/a.py"]
        # HEAD must be restored to the caller's original branch, not left on main.
        assert state["branch"] == "feature/work"

    def test_restores_branch_on_sequential_skip(self, tmp_path, monkeypatch):
        fake_run_git, state, _calls = self._git_recorder(merge_ok=False)
        monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)
        monkeypatch.setattr(wt_mod, "detect_merge_conflicts", lambda *_a, **_k: ["src/a.py"])
        monkeypatch.setattr(wt_mod, "_is_trivial_conflict", lambda *_a, **_k: False)

        report = merge_with_conflict_report(tmp_path, "src", "main", strategy="sequential")

        assert report["success"] is False
        assert report.get("retry_sequential") is True
        assert state["branch"] == "feature/work"

    def test_successful_merge_stays_on_target(self, tmp_path, monkeypatch):
        fake_run_git, state, _calls = self._git_recorder(merge_ok=True)
        monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)
        monkeypatch.setattr(wt_mod, "detect_merge_conflicts", lambda *_a, **_k: [])

        report = merge_with_conflict_report(tmp_path, "src", "main", strategy="prefer_source")

        assert report["success"] is True
        # A successful merge legitimately leaves HEAD on the target branch.
        assert state["branch"] == "main"

    def test_failed_merge_restores_branch(self, tmp_path, monkeypatch):
        fake_run_git, state, _calls = self._git_recorder(merge_ok=False)
        monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)
        monkeypatch.setattr(wt_mod, "detect_merge_conflicts", lambda *_a, **_k: [])

        report = merge_with_conflict_report(tmp_path, "src", "main", strategy="prefer_source")

        assert report["success"] is False
        # Merge failed mid-way -> restore the caller's branch.
        assert state["branch"] == "feature/work"

    def test_detached_head_no_restore_attempt(self, tmp_path, monkeypatch):
        # rev-parse returns "HEAD" (detached) -> nothing safe to restore to.
        fake_run_git, _state, calls = self._git_recorder(merge_ok=False, start_branch="HEAD")
        monkeypatch.setattr(wt_mod, "_run_git", fake_run_git)
        monkeypatch.setattr(wt_mod, "detect_merge_conflicts", lambda *_a, **_k: ["x"])
        monkeypatch.setattr(wt_mod, "_is_trivial_conflict", lambda *_a, **_k: False)

        report = merge_with_conflict_report(tmp_path, "src", "main", strategy="manual")

        assert report["success"] is False
        # Only the target checkout happened; no restore checkout to "HEAD".
        checkout_targets = [a[-1] for a in calls if a[0] == "checkout"]
        assert checkout_targets == ["main"]
