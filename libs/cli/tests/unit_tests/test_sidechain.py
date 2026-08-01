"""Unit tests for the sidechain transcript store and `/btw` command glue."""

from __future__ import annotations

import asyncio
import threading

import pytest

from bog_agents_cli.sidechain import (
    SidechainStore,
    build_continuation_prompt,
    handle_btw_command,
)


class _FakeSettings:
    """Stub exposing just ``user_agents_dir`` for `/btw` handler tests."""

    def __init__(self, config_dir) -> None:
        self.user_agents_dir = config_dir


class _FakeApp:
    """Minimal stand-in for BogAgentsApp: mounts messages, exposes a thread id."""

    def __init__(self, config_dir, thread_id: str | None = "thread-abc") -> None:
        self.settings = _FakeSettings(config_dir)
        self._thread_id = thread_id
        self.mounted: list[object] = []

    async def _mount_message(self, widget) -> None:
        self.mounted.append(widget)

    def _current_thread_id(self) -> str | None:
        return self._thread_id


@pytest.fixture
def store(tmp_path):
    """A SidechainStore rooted in a fresh temp directory."""
    return SidechainStore(tmp_path)


class TestSidechainStore:
    def test_record_and_load_round_trip(self, store) -> None:
        store.record("thread-1", "note", "remember to pin ruff")
        records = store.load("thread-1")
        assert len(records) == 1
        assert records[0].agent_id == "thread-1"
        assert records[0].kind == "note"
        assert records[0].content == "remember to pin ruff"
        assert records[0].parent_thread_id is None

    def test_load_empty_for_unknown_agent(self, store) -> None:
        assert store.load("ghost") == []

    def test_records_persist_across_store_instances(self, tmp_path) -> None:
        first = SidechainStore(tmp_path)
        first.record("bg-001", "result", "all green")
        second = SidechainStore(tmp_path)
        records = second.load("bg-001")
        assert len(records) == 1
        assert records[0].content == "all green"

    def test_record_carries_parent_thread_id(self, store) -> None:
        store.record("bg-002", "result", "done", parent_thread_id="thread-xyz")
        (record,) = store.load("bg-002")
        assert record.parent_thread_id == "thread-xyz"

    def test_append_order_preserved(self, store) -> None:
        for i in range(3):
            store.record("t1", "note", f"note {i}")
        contents = [r.content for r in store.load("t1")]
        assert contents == ["note 0", "note 1", "note 2"]

    def test_agent_ids_lists_sorted(self, store) -> None:
        store.record("bg-003", "note", "a")
        store.record("bg-001", "note", "b")
        store.record("bg-002", "note", "c")
        assert store.agent_ids() == ["bg-001", "bg-002", "bg-003"]

    def test_agent_ids_empty_before_any_write(self, store) -> None:
        assert store.agent_ids() == []

    def test_unsafe_agent_id_is_sanitized(self, store) -> None:
        store.record("../evil", "note", "boom")
        records = store.load("../evil")
        assert len(records) == 1
        assert records[0].agent_id == "../evil"
        assert store.record_path("../evil").name == "evil.jsonl"
        assert ".." not in str(store.record_path("../evil"))

    def test_corrupt_line_is_skipped(self, store) -> None:
        store.record("t1", "note", "good")
        path = store.record_path("t1")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{not valid json}\n")
        store.record("t1", "note", "also good")
        records = store.load("t1")
        assert [r.content for r in records] == ["good", "also good"]

    def test_transcript_text_rendering(self, store) -> None:
        store.record("t1", "submission", "fix lint")
        store.record("t1", "result", "done")
        text = store.transcript_text("t1")
        assert "[submission] fix lint" in text
        assert "[result] done" in text

    def test_transcript_text_limit(self, store) -> None:
        for i in range(5):
            store.record("t1", "note", f"n{i}")
        text = store.transcript_text("t1", limit=2)
        assert "[note] n3" in text
        assert "[note] n0" not in text

    def test_concurrent_appends_are_safe(self, tmp_path) -> None:
        store = SidechainStore(tmp_path)
        errors: list[Exception] = []

        def writer(index: int) -> None:
            try:
                for _ in range(25):
                    store.record("t1", "note", f"writer-{index}")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(store.load("t1")) == 100

    def test_injected_clock_controls_timestamps(self, tmp_path) -> None:
        store = SidechainStore(tmp_path, now=lambda: 42.0)
        store.record("t1", "note", "x")
        (record,) = store.load("t1")
        assert record.ts == 42.0


class TestBuildContinuationPrompt:
    def test_no_records_returns_instruction_unchanged(self, tmp_path) -> None:
        prompt = build_continuation_prompt("ghost", tmp_path, "keep going")
        assert prompt == "keep going"

    def test_includes_transcript_and_instruction(self, store, tmp_path) -> None:
        store.record("bg-001", "submission", "investigate flaky test")
        store.record("bg-001", "result", "root cause: cache stampede")
        prompt = build_continuation_prompt("bg-001", tmp_path, "write the fix")
        assert "bg-001" in prompt
        assert "cache stampede" in prompt
        assert "Instruction: write the fix" in prompt

    def test_huge_transcript_is_truncated(self, store, tmp_path) -> None:
        store.record("bg-001", "note", "z" * 60_000)
        prompt = build_continuation_prompt("bg-001", tmp_path, "go")
        assert len(prompt) < 60_000


class TestSidechainContinuationPrompt:
    def test_store_method_matches_module_function(self, store, tmp_path) -> None:
        store.record("bg-001", "submission", "investigate flaky test")
        store.record("bg-001", "result", "root cause: cache stampede")
        from_store = store.continuation_prompt("bg-001", "write the fix")
        from_module = build_continuation_prompt("bg-001", tmp_path, "write the fix")
        assert from_store == from_module
        assert "cache stampede" in from_store
        assert "Instruction: write the fix" in from_store

    def test_no_records_returns_instruction(self, store) -> None:
        assert store.continuation_prompt("ghost", "keep going") == "keep going"

    def test_truncates_huge_transcript(self, store) -> None:
        store.record("bg-001", "note", "z" * 60_000)
        prompt = store.continuation_prompt("bg-001", "go")
        assert len(prompt) < 60_000


class TestHandleBtwCommand:
    def _patch_settings(self, monkeypatch, tmp_path) -> None:
        from bog_agents_cli import config as config_module

        monkeypatch.setattr(config_module, "settings", _FakeSettings(tmp_path))

    async def test_records_note_for_current_thread(self, tmp_path, monkeypatch) -> None:
        self._patch_settings(monkeypatch, tmp_path)
        app = _FakeApp(tmp_path, thread_id="thread-abc")
        await handle_btw_command(app, "/btw pin the ruff version")
        (record,) = SidechainStore(tmp_path).load("thread-abc")
        assert record.kind == "note"
        assert record.content == "pin the ruff version"

    async def test_empty_note_shows_usage_and_records_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        self._patch_settings(monkeypatch, tmp_path)
        app = _FakeApp(tmp_path, thread_id="thread-abc")
        await handle_btw_command(app, "/btw")
        assert SidechainStore(tmp_path).load("thread-abc") == []
        assert len(app.mounted) == 2
        assert "Usage: /btw" in str(app.mounted[1]._content)

    async def test_uses_interactive_fallback_without_thread(
        self, tmp_path, monkeypatch
    ) -> None:
        self._patch_settings(monkeypatch, tmp_path)
        app = _FakeApp(tmp_path, thread_id=None)
        await handle_btw_command(app, "/btw hello")
        (record,) = SidechainStore(tmp_path).load("interactive")
        assert record.content == "hello"

    async def test_mounts_user_message_and_confirmation(
        self, tmp_path, monkeypatch
    ) -> None:
        self._patch_settings(monkeypatch, tmp_path)
        app = _FakeApp(tmp_path, thread_id="thread-abc")
        await handle_btw_command(app, "/btw some note")
        assert len(app.mounted) == 2
        assert "thread-abc" in str(app.mounted[1]._content)
        assert ".jsonl" in str(app.mounted[1]._content)


def test_async_handle_btw_command_runs_under_asyncio(tmp_path, monkeypatch) -> None:
    """Sanity check that the handler is awaitable from a real event loop."""
    from bog_agents_cli import config as config_module

    monkeypatch.setattr(config_module, "settings", _FakeSettings(tmp_path))
    app = _FakeApp(tmp_path, thread_id="thread-loop")
    asyncio.run(handle_btw_command(app, "/btw loop note"))
    (record,) = SidechainStore(tmp_path).load("thread-loop")
    assert record.content == "loop note"


class TestBackgroundTaskSidechain:
    """Background tasks append their lifecycle to a shared sidechain store."""

    async def test_submission_and_result_recorded(self, tmp_path) -> None:
        from bog_agents_cli.background_agents import BackgroundAgentManager

        store = SidechainStore(tmp_path)
        manager = BackgroundAgentManager(sidechain_store=store)

        async def runner(task) -> dict:  # type: ignore[no-untyped-def]
            return {"messages": [{"role": "assistant", "content": "all done"}]}

        task_id = await manager.submit(
            "investigate", parent_thread_id="parent-1", runner=runner
        )
        task = manager.get_status(task_id)
        assert task is not None
        await task._task

        records = store.load(task_id)
        assert [r.kind for r in records] == ["submission", "result"]
        assert records[0].content == "investigate"
        assert records[0].parent_thread_id == "parent-1"
        assert records[1].content == "all done"
        assert records[1].parent_thread_id == "parent-1"

    async def test_error_recorded_on_failure(self, tmp_path) -> None:
        from bog_agents_cli.background_agents import BackgroundAgentManager

        store = SidechainStore(tmp_path)
        manager = BackgroundAgentManager(sidechain_store=store)

        async def failing_runner(task) -> None:
            raise RuntimeError("boom")

        task_id = await manager.submit("risky", runner=failing_runner)
        task = manager.get_status(task_id)
        assert task is not None
        await task._task

        records = store.load(task_id)
        assert [r.kind for r in records] == ["submission", "error"]
        assert "boom" in records[1].content

    async def test_cancelled_recorded(self, tmp_path) -> None:
        from bog_agents_cli.background_agents import (
            BackgroundAgentManager,
            BackgroundStatus,
        )

        store = SidechainStore(tmp_path)
        manager = BackgroundAgentManager(sidechain_store=store)

        async def slow_runner(task) -> None:
            await asyncio.sleep(30)

        task_id = await manager.submit("slow", runner=slow_runner)
        task = manager.get_status(task_id)
        assert task is not None
        for _ in range(100):
            if task.status == BackgroundStatus.RUNNING:
                break
            await asyncio.sleep(0.01)
        assert manager.cancel(task_id)
        await task._task

        records = store.load(task_id)
        assert records[-1].kind == "cancelled"

    async def test_no_store_is_a_noop(self) -> None:
        from bog_agents_cli.background_agents import BackgroundAgentManager

        manager = BackgroundAgentManager()

        async def runner(task) -> str:
            return "fine"

        task_id = await manager.submit("x", runner=runner)
        task = manager.get_status(task_id)
        assert task is not None
        await task._task
        assert task.result == "fine"


class TestBackgroundContinueFrom:
    """AgentId continuation: `continue_from` seeds the prompt with a sidechain."""

    async def test_continue_from_seeds_prompt_and_metadata(self, tmp_path) -> None:
        from bog_agents_cli.background_agents import BackgroundAgentManager

        store = SidechainStore(tmp_path)
        store.record("bg-050", "submission", "investigate slow build")
        store.record("bg-050", "result", "root cause: cache stampede")
        manager = BackgroundAgentManager(sidechain_store=store)

        async def runner(task) -> dict:
            return {"messages": [{"role": "assistant", "content": "done"}]}

        task_id = await manager.submit(
            "apply the fix", continue_from="bg-050", runner=runner
        )
        task = manager.get_status(task_id)
        assert task is not None
        assert task.metadata["continue_from"] == "bg-050"
        assert task.metadata["original_prompt"] == "apply the fix"
        assert "cache stampede" in task.prompt
        assert "Continue the recorded sidechain for agent 'bg-050'" in task.prompt
        await task._task

        # The composed transcript is derivable from the source sidechain, so the
        # new task's own submission record keeps the original instruction.
        records = store.load(task_id)
        assert records[0].content == "apply the fix"

    async def test_continue_from_without_store_is_noop(self) -> None:
        from bog_agents_cli.background_agents import BackgroundAgentManager

        manager = BackgroundAgentManager()

        async def runner(task) -> str:
            return "ok"

        task_id = await manager.submit("x", continue_from="bg-050", runner=runner)
        task = manager.get_status(task_id)
        assert task is not None
        assert "continue_from" not in task.metadata
        assert task.prompt == "x"
        await task._task

    async def test_continue_from_unknown_agent_keeps_instruction(
        self, tmp_path
    ) -> None:
        from bog_agents_cli.background_agents import BackgroundAgentManager

        store = SidechainStore(tmp_path)
        manager = BackgroundAgentManager(sidechain_store=store)

        async def runner(task) -> str:
            return "ok"

        task_id = await manager.submit(
            "plain instruction", continue_from="bg-999", runner=runner
        )
        task = manager.get_status(task_id)
        assert task is not None
        assert task.prompt == "plain instruction"
        await task._task
