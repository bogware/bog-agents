"""ROADMAP #71: /subtask and /fork hand background forks to the manager with a conversation brief."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bog_agents_cli import fork_controller as fc


class _Manager:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit(self, prompt: str, **kwargs: Any) -> str:
        self.submitted.append({"prompt": prompt, **kwargs})
        return f"bg-{len(self.submitted)}"


class _Store:
    def __init__(self, messages: list[tuple[str, str]]) -> None:
        self._messages = [
            SimpleNamespace(type=kind, content=content) for kind, content in messages
        ]

    def get_all_messages(self) -> list[Any]:
        return list(self._messages)


class FakeApp:
    def __init__(self, tmp_path: Path) -> None:
        self._cwd = str(tmp_path)
        self._lc_thread_id = "thread-9"
        self._bg_manager = _Manager()
        self._message_store = _Store(
            [
                ("user", "fix the flaky test"),
                ("assistant", "I found the race in worker.py"),
                ("app", "ignored"),
                ("user", "great"),
            ]
        )
        self.mounted: list[str] = []
        self.agent_commands: list[str] = []

    async def _mount_message(self, message: object) -> None:
        self.mounted.append(
            str(
                getattr(message, "_content", None)
                or getattr(message, "content", None)
                or message
            )
        )

    async def _handle_agent_command(self, command: str, *, echo: bool = True) -> None:
        self.agent_commands.append(command)


@pytest.fixture(autouse=True)
def _forks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fc, "forks_dir", lambda: tmp_path / "cfg")


def test_brief_keeps_user_and_assistant_turns_only(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    brief = fc.conversation_brief(app)
    assert brief.splitlines() == [
        "User: fix the flaky test",
        "Assistant: I found the race in worker.py",
        "User: great",
    ]
    assert fc.conversation_brief(SimpleNamespace()) == ""
    assert fc.fork_prompt("", "do it") == "do it"
    assert "recent context" in fc.fork_prompt("User: hi", "do it")


async def test_subtask_submits_with_the_brief(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    await fc.run_fork_command(app, "/subtask")
    assert fc.USAGE_SUBTASK in app.mounted[-1]
    await fc.run_fork_command(app, "/subtask write the regression test")
    job = app._bg_manager.submitted[-1]
    assert (
        job["label"] == "subtask"
        and job["parent_thread_id"] == "thread-9"
        and job["working_dir"] == str(tmp_path)
    )
    assert (
        job["prompt"].endswith("write the regression test")
        and "I found the race" in job["prompt"]
    )
    assert job["metadata"]["original_prompt"] == "write the regression test"
    assert "Subtask bg-1 started" in app.mounted[-1]


async def test_fork_records_and_runs_in_background_or_worktree(tmp_path: Path) -> None:
    from bog_agents_cli.session_fork import list_forks

    app = FakeApp(tmp_path)
    await fc.run_fork_command(app, "/fork try the async approach")
    job = app._bg_manager.submitted[-1]
    assert job["label"].startswith("fork-") and job["metadata"]["kind"] == "fork"
    assert (
        "try the async approach" in job["prompt"]
        and "I found the race" in job["prompt"]
    )
    forks = list_forks(tmp_path / "cfg", "thread-9")
    assert len(forks) == 1 and forks[0].name == "try the async approach"
    assert "background agent" in app.mounted[-1]

    await fc.run_fork_command(app, "/fork --worktree spike")
    assert len(app._bg_manager.submitted) == 1  # worktree forks go through /agent spawn
    assert app.agent_commands and app.agent_commands[-1].startswith(
        "/agent spawn --worktree --label fork-"
    )
    assert "fresh worktree" in app.mounted[-1]
    assert len(list_forks(tmp_path / "cfg", "thread-9")) == 2


async def test_without_a_manager(tmp_path: Path) -> None:
    app = FakeApp(tmp_path)
    app._bg_manager = None  # type: ignore[assignment]
    await fc.run_fork_command(app, "/subtask x")
    assert "not available" in app.mounted[-1]
