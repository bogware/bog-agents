"""ROADMAP #68: the `/tasks` command center, team run handles, queue verbs and `/recap`."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from bog_agents_cli import tasks_controller as tc
from bog_agents_cli.team_executor import parse_team_run_args, run_team_session


@dataclass
class _Task:
    task_id: str
    label: str = ""
    prompt: str = ""
    status: str = "running"
    started_at: float | None = 1000.0
    completed_at: float | None = None
    worktree_branch: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class _Manager:
    def __init__(self, *tasks: _Task) -> None:
        self.all_tasks = list(tasks)
        self.cancelled: list[str] = []

    def get_status(self, task_id: str) -> _Task | None:
        return next((t for t in self.all_tasks if t.task_id == task_id), None)

    def cancel(self, task_id: str) -> bool:
        task = self.get_status(task_id)
        if task is None or task.status != "running":
            return False
        self.cancelled.append(task_id)
        task.status = "cancelled"
        return True


class _Mount:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def __call__(self, widget: object) -> None:
        self.messages.append(widget)

    @property
    def text(self) -> str:
        return "\n".join(str(m) for m in self.messages)


def _app(**overrides: Any) -> SimpleNamespace:
    app = SimpleNamespace(
        _pending_approval_widget=None,
        _turns=SimpleNamespace(busy=False, agent_worker=None),
        _token_tracker=SimpleNamespace(current_context=12_000),
        _session_stats=SimpleNamespace(
            request_count=4,
            input_tokens=9000,
            output_tokens=1200,
            file_records=[1, 2],
            per_model={},
        ),
        _pending_messages=deque(),
        _queued_widgets=deque(),
        _bg_manager=_Manager(
            _Task("bg-1", label="Refactor X", worktree_branch="feat/x"),
            _Task("bg-2", label="Old", status="completed"),
        ),
        _remote_tasks={},
        _team_runs={},
        _current_thread_id=lambda: "abc123def456",
        _mount_message=_Mount(),
    )
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


@pytest.fixture
def plain_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the message widgets plain strings so the dispatcher is testable without Textual."""
    from bog_agents_cli.widgets import messages

    monkeypatch.setattr(messages, "AppMessage", lambda text: f"APP:{text}")
    monkeypatch.setattr(messages, "DiffMessage", lambda text: f"DIFF:{text}")
    monkeypatch.setattr(messages, "UserMessage", lambda text: f"USER:{text}")


class TestTree:
    async def test_waiting_queue_and_background_render(self) -> None:
        app = _app()
        app._pending_approval_widget = SimpleNamespace(
            _action_requests=[{"name": "execute", "args": {"command": "pytest -q"}}]
        )
        from bog_agents_cli.app import QueuedMessage

        app._pending_messages.extend(
            [
                QueuedMessage(text="fix the tests", mode="normal"),
                QueuedMessage(text="then commit", mode="normal"),
            ]
        )
        root = await tc.build_task_tree(app, include_daemon=False)
        main = tc.find_node(root, "main")
        assert (
            main is not None and main.status == "waiting" and "pytest -q" in main.detail
        )
        assert [n.id for n in main.children] == ["q1", "q2"]
        bg = tc.find_node(root, "bg-1")
        assert (
            bg is not None
            and bg.status == "running"
            and "worktree feat/x" in bg.detail
            and "diff" in bg.actions
        )
        assert tc.find_node(root, "bg-2").status == "done"
        text = tc.render_task_tree(root)
        assert (
            "waiting on you" in text
            and "q1" in text
            and "bg-1" in text
            and "/tasks kill <id>" in text
        )
        assert tc.find_node(root, "bg") is None  # ambiguous prefix
        assert tc.find_node(root, "bg-2") is not None

    def test_normalize_status_covers_every_vocabulary(self) -> None:
        assert tc.normalize_status("completed") == "done"
        assert tc.normalize_status("claimed") == "running"
        assert tc.normalize_status(SimpleNamespace(value="FAILED")) == "failed"
        assert tc.normalize_status("open") == "queued"


class TestVerbs:
    async def test_kill_steer_and_queue(self) -> None:
        app = _app()
        root = await tc.build_task_tree(app, include_daemon=False)
        assert "Cancel requested" in await tc.kill_node(app, tc.find_node(root, "bg-1"))
        assert app._bg_manager.cancelled == ["bg-1"]
        assert "not running" in await tc.kill_node(app, tc.find_node(root, "bg-2"))
        message = tc.steer_node(app, tc.find_node(root, "bg-1"), "stop and report")
        assert "inbox 1" in message
        assert (
            app._bg_manager.get_status("bg-1").metadata["inbox"][0]["body"]
            == "stop and report"
        )
        assert "Queued as q1" in tc.steer_node(
            app, tc.find_node(root, "main"), "also run lint"
        )
        assert app._pending_messages[0].text == "also run lint"
        assert "q1 now" in tc.edit_queued(app, 1, "run lint first")
        assert app._pending_messages[0].text == "run lint first"
        assert "No queued prompt q5" in tc.drop_queued(app, 5)
        assert "Dropped q1" in tc.drop_queued(app, 1)
        assert not app._pending_messages

    async def test_dispatcher(self, plain_messages: None) -> None:
        app = _app()
        await tc.run_tasks_command(app, "/tasks")
        assert (
            "Tasks —" in app._mount_message.text and "bg-1" in app._mount_message.text
        )
        await tc.run_tasks_command(app, "/tasks kill bg-1")
        assert "Cancel requested for bg-1" in app._mount_message.text
        await tc.run_tasks_command(app, "/tasks steer nope hi")
        assert "No task 'nope'" in app._mount_message.text
        await tc.run_tasks_command(app, "/tasks queue")
        assert "Nothing queued" in app._mount_message.text
        await tc.run_tasks_command(app, "/tasks pause bg-1")
        assert "Only team runs can be paused" in app._mount_message.text


class TestTeamRuns:
    async def test_handle_pause_steer_and_finish(self) -> None:
        app = _app()
        req = parse_team_run_args("write tests | fix lint")
        assert len(req.task_specs) == 2
        handle = tc.register_team_run(app, req.task_specs, ["w1", "w2"])
        assert handle.run_id in app._team_runs and not handle.paused
        root = await tc.build_task_tree(app, include_daemon=False)
        team = tc.find_node(root, handle.run_id)
        assert team is not None and len(team.children) == 2 and team.status == "running"
        assert "pause" in team.actions
        assert "Paused" in tc.set_paused(app, team, paused=True)
        assert handle.paused
        root = await tc.build_task_tree(app, include_daemon=False)
        assert tc.find_node(root, handle.run_id).status == "paused"
        assert "Resumed" in tc.set_paused(app, team, paused=False)
        assert "team mailbox" in tc.steer_node(app, team, "prefer small commits")
        assert handle.mailbox.inbox("w1")[0].body == "prefer small commits"
        tc.finish_team_run(handle, status="done", report=None)
        root = await tc.build_task_tree(app, include_daemon=False)
        assert tc.find_node(root, handle.run_id).status == "done"

    async def test_run_team_session_respects_pause_gate(self) -> None:
        req = parse_team_run_args("alpha | beta")
        gate = asyncio.Event()  # cleared: paused from the start
        seen: list[str] = []

        async def runner(member: str, task: object, mailbox: object) -> object:
            from bog_agents.teams import TaskResult

            seen.append(task.title)
            return TaskResult(success=True, result=f"{member}:{task.title}")

        from pathlib import Path

        session = asyncio.create_task(
            run_team_session(
                req.task_specs,
                ["w1"],
                repo_dir=Path.cwd(),
                teammate_runner=runner,
                pause_gate=gate,
            )
        )
        await asyncio.sleep(0.05)
        assert seen == [] and not session.done()
        gate.set()
        report = await asyncio.wait_for(session, timeout=5)
        assert report.all_done and sorted(seen) == ["alpha", "beta"]


class TestRecap:
    async def test_recap_lists_needs_you_and_notes(self) -> None:
        app = _app()
        app._pending_approval_widget = SimpleNamespace(
            _action_requests=[{"name": "write_file", "args": {}}]
        )
        app._bg_manager.all_tasks.append(
            _Task("bg-3", label="Broken", status="failed", error="boom")
        )
        root = await tc.build_task_tree(app, include_daemon=False)
        text = tc.build_recap(
            app, root, notes=[SimpleNamespace(content="remember to bump the version")]
        )
        assert (
            "## Recap" in text
            and "4 model requests" in text
            and "2 file change(s)" in text
        )
        assert (
            "### Needs you" in text and "approve write_file" in text and "bg-3" in text
        )
        assert "### Notes (/btw)" in text and "bump the version" in text
