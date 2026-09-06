"""ROADMAP #76 wiring: persistent team mailbox, `[worktree] reuse`, `/worktree create` reuse, teammate file tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from bog_agents.sandbox_config import load_sandbox_config

if TYPE_CHECKING:
    import pytest

REUSE_TOML = """[sandbox]
runner_size = "small"

[worktree]
reuse = ["node_modules", ".venv"]
"""
PLAIN_TOML = """[sandbox]
runner_size = "small"
"""


def test_team_mailbox_persists_per_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents.mailbox_store import MailboxStore
    from bog_agents.teams import Mailbox

    from bog_agents_cli import _env_vars, tasks_controller

    monkeypatch.setattr(_env_vars, "bog_agents_home", lambda: tmp_path / "home")

    class _App:
        def __init__(self, thread: str | None) -> None:
            self._thread = thread

        def _current_thread_id(self) -> str | None:
            return self._thread

    assert isinstance(tasks_controller._team_mailbox(_App(None)), Mailbox)
    box = tasks_controller._team_mailbox(_App("thread/ab c"))
    assert (
        isinstance(box, MailboxStore)
        and box.path == tmp_path / "home" / "mailboxes" / "thread_ab_c.db"
    )
    box.send("supervisor", "worker-1", "carry on")
    again = tasks_controller._team_mailbox(_App("thread/ab c"))
    assert [m.body for m in again.drain("worker-1")] == ["carry on"]


def test_worktree_reuse_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".bog-agents").mkdir(parents=True)
    (project / ".bog-agents" / "sandbox.toml").write_text(REUSE_TOML, encoding="utf-8")
    config = load_sandbox_config(project)
    assert config is not None and config.worktree_reuse == ["node_modules", ".venv"]
    plain = tmp_path / "plain"
    (plain / ".bog-agents").mkdir(parents=True)
    (plain / ".bog-agents" / "sandbox.toml").write_text(PLAIN_TOML, encoding="utf-8")
    loaded = load_sandbox_config(plain)
    assert loaded is not None and loaded.worktree_reuse == []


def test_create_worktree_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bog_agents.middleware import worktree as wt

    from bog_agents_cli import envcache, worktrees_controller

    class _Info:
        path = tmp_path / "wt"
        branch = "feature/x"

    monkeypatch.setattr(wt, "create_worktree", lambda _repo, _branch: _Info())
    monkeypatch.setattr(envcache, "configured_reuse", lambda: ("node_modules",))
    monkeypatch.setattr(
        envcache,
        "reuse_into_worktree",
        lambda _r, _w, _reuse: ["node_modules: junction -> cache"],
    )
    text, ok = asyncio.run(
        worktrees_controller.create_worktree_report(tmp_path, "feature/x")
    )
    assert ok and "feature/x" in text and "node_modules: junction" in text
    assert asyncio.run(worktrees_controller.create_worktree_report(tmp_path, "")) == (
        "Usage: /worktree create <branch>",
        False,
    )

    def _bad(_repo: Path, _branch: str) -> NoReturn:
        msg = "bad ref"
        raise ValueError(msg)

    monkeypatch.setattr(wt, "create_worktree", _bad)
    text, ok = asyncio.run(worktrees_controller.create_worktree_report(tmp_path, "x y"))
    assert not ok and "Invalid branch name" in text


def test_teammate_runner_passes_file_tools(tmp_path: Path) -> None:
    from bog_agents.teams import LedgerTask, Mailbox

    from bog_agents_cli.team_executor import build_worktree_teammate_runner

    seen: dict[str, Any] = {}

    class _Agent:
        async def ainvoke(self, _payload: dict[str, Any]) -> dict[str, Any]:
            return {"messages": []}

    def factory(_model: object, **kwargs: Any) -> tuple[_Agent, None]:
        seen.update(kwargs)
        return _Agent(), None

    runner = build_worktree_teammate_runner(
        repo_dir=tmp_path,
        resolve_model=lambda s: s,
        model_spec="m",
        agent_factory=factory,
    )
    asyncio.run(runner("worker-1", LedgerTask(id="t1", title="do"), Mailbox()))
    assert sorted(t.name for t in seen["extra_tools"]) == [
        "receive_files",
        "send_file",
        "send_patch",
    ]


def test_add_dir_mounts_become_routes(tmp_path: Path) -> None:
    from bog_agents_cli import mounts

    other = tmp_path / "other"
    other.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    mounts.add_mount(project, str(other), name="other")
    assert mounts.mount_routes(project) == {"/mnt/other/": other.resolve()}
