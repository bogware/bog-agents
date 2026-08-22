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
import subprocess
from pathlib import Path

import pytest
from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore

from bog_agents.middleware import worktree as wt_mod
from bog_agents.middleware.worktree import (
    ParallelWorktreeMiddleware,
    WorktreeInfo,
    WorktreeMiddleware,
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
            if args[0] in ("checkout", "switch"):
                # ("checkout", branch) for the target park, ("switch", branch)
                # for the SB-2-fixed restore path.
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
        # Only the target checkout happened; no restore switch to "HEAD".
        checkout_targets = [a[-1] for a in calls if a[0] in ("checkout", "switch")]
        assert checkout_targets == ["main"]


# --------------------------------------------------------------------------- #
# SB-2 (v5, = v4 SB-3) — real-repo proof that branch switches actually switch
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    """Run git in `repo`, raising on failure, and return stripped stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    """A real git repository on branch `main` with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        _git(repo, "init", "-b", "main")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git unavailable (or too old for `init -b`) in this environment")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Worktree Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


class TestMergeWorktreeRealRepo:
    """SB-2: `checkout -- <branch>` treated the branch as a pathspec, so
    `merge_worktree` failed on every normal repo and the P26 restore net never
    restored. These tests drive the real tools against a real git repo.
    """

    def _merge_tool(self, repo: Path):
        mw = WorktreeMiddleware(working_dir=repo)
        return next(t for t in mw.tools if t.name == "merge_worktree")

    def test_merge_worktree_merges_feature_into_target(self, real_repo: Path) -> None:
        _git(real_repo, "switch", "-c", "feature")
        (real_repo / "feat.txt").write_text("feature\n", encoding="utf-8")
        _git(real_repo, "add", "feat.txt")
        _git(real_repo, "commit", "-m", "feat")

        result = self._merge_tool(real_repo).func(_make_runtime(), "feature", "main")

        assert "Failed to checkout" not in result
        assert result.startswith("Merge result:")
        # The tool really switched to the target and the change really merged.
        assert _git(real_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert (real_repo / "feat.txt").read_text(encoding="utf-8") == "feature\n"

    def test_merge_worktree_branch_not_shadowed_by_tracked_file(self, real_repo: Path) -> None:
        # A tracked file literally named `main` used to satisfy the pathspec
        # form (`checkout -- main` restored the FILE), so the merge silently
        # landed on whatever branch the tree was parked on. `git switch` never
        # takes pathspecs, so the branch always wins.
        (real_repo / "main").write_text("decoy\n", encoding="utf-8")
        _git(real_repo, "add", "main")
        _git(real_repo, "commit", "-m", "decoy file named main")
        _git(real_repo, "switch", "-c", "feature")
        (real_repo / "feat.txt").write_text("feature\n", encoding="utf-8")
        _git(real_repo, "add", "feat.txt")
        _git(real_repo, "commit", "-m", "feat")
        _git(real_repo, "switch", "main")
        _git(real_repo, "switch", "-c", "other")

        result = self._merge_tool(real_repo).func(_make_runtime(), "feature", "main")

        assert result.startswith("Merge result:")
        assert _git(real_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert (real_repo / "feat.txt").exists()
        # The parked branch must NOT have received the merge.
        _git(real_repo, "switch", "other")
        assert not (real_repo / "feat.txt").exists()

    def test_restore_branch_returns_to_original(self, real_repo: Path) -> None:
        # main and feature conflict on base.txt; the caller is parked on `work`.
        _git(real_repo, "switch", "-c", "feature")
        (real_repo / "base.txt").write_text("feature version\n", encoding="utf-8")
        _git(real_repo, "commit", "-am", "feature change")
        _git(real_repo, "switch", "main")
        (real_repo / "base.txt").write_text("main version\n", encoding="utf-8")
        _git(real_repo, "commit", "-am", "main change")
        _git(real_repo, "switch", "-c", "work")

        report = merge_with_conflict_report(real_repo, "feature", "main", strategy="manual")

        assert report["success"] is False
        # The P26 safety net actually restores now: HEAD is back on `work`
        # (aborting the dangling failed merge on the way), not left parked on
        # the merge target mid-merge.
        assert _git(real_repo, "rev-parse", "--abbrev-ref", "HEAD") == "work"
        # No merge left in progress on the restored branch.
        assert not (real_repo / ".git" / "MERGE_HEAD").exists()
        # And the parked branch's file content is untouched.
        assert (real_repo / "base.txt").read_text(encoding="utf-8") == "main version\n"
