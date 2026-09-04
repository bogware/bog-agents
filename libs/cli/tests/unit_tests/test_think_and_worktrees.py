"""`/think` and `/worktrees` reach real machinery (v6 CLI-1 = v4 P1-25 + P1-32).

Both handlers scanned `self._middleware` for an in-process middleware. The
TUI's agent runs in the LangGraph server process, so that attribute was never
assigned and every subcommand printed "… is not active in this session" for
three review cycles while `/help` advertised them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bog_agents_cli.app import BogAgentsApp
from bog_agents_cli.widgets.messages import AppMessage


def _messages_containing(app: BogAgentsApp, text: str) -> list[AppMessage]:
    return [w for w in app.query(AppMessage) if text in str(w._content)]


class TestThink:
    async def test_on_off_toggle_flow_through_runtime_context(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._build_cli_context().get("thinking_enabled") is None

            await app._handle_think_command("/think on")
            await pilot.pause()
            assert app._thinking_enabled is True
            assert app._build_cli_context()["thinking_enabled"] is True
            assert _messages_containing(app, "Extended thinking enabled")

            await app._handle_think_command("/think budget 16000")
            await pilot.pause()
            assert app._build_cli_context()["thinking_budget_tokens"] == 16000

            await app._handle_think_command("/think off")
            await pilot.pause()
            assert app._build_cli_context()["thinking_enabled"] is False

            await app._handle_think_command("/think toggle")
            await pilot.pause()
            assert app._thinking_enabled is True

    async def test_status_never_claims_not_active(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._handle_think_command("/think")
            await pilot.pause()
            assert not _messages_containing(app, "not active")
            assert _messages_containing(app, "Extended thinking:")

    async def test_budget_validation(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._handle_think_command("/think budget 12")
            await app._handle_think_command("/think budget lots")
            await pilot.pause()
            assert app._thinking_budget_tokens is None
            assert _messages_containing(app, "at least 1024")
            assert _messages_containing(app, "Invalid budget value")


class TestWorktrees:
    async def test_status_without_tasks_is_honest(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._handle_worktrees_command("/worktrees")
            await pilot.pause()
            assert not _messages_containing(app, "not active")
            assert _messages_containing(app, "No worktree tasks")

    async def test_cancel_stops_the_worker(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._ensure_background_manager()
            task = MagicMock(status="running", worktree_branch="agent/x-abc123")
            app._bg_manager.get_status = MagicMock(return_value=task)  # type: ignore[method-assign]
            app._bg_manager.cancel = MagicMock(return_value=True)  # type: ignore[method-assign]

            await app._handle_worktrees_command("/worktrees cancel task-1")
            await pilot.pause()

            app._bg_manager.cancel.assert_called_once_with("task-1")
            assert _messages_containing(app, "cancelled (worker stopped)")

    async def test_spawn_delegates_to_worktree_agents(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_agent_command = AsyncMock()  # type: ignore[method-assign]

            await app._handle_worktrees_command(
                '/worktrees spawn [{"label": "tests", "prompt": "add tests"}, {"prompt": "fix lint"}]'
            )
            await pilot.pause()

            calls = [c.args[0] for c in app._handle_agent_command.await_args_list]
            assert calls == [
                "/agent spawn --worktree --label tests add tests",
                "/agent spawn --worktree fix lint",
            ]
            assert all(
                c.kwargs == {"echo": False}
                for c in app._handle_agent_command.await_args_list
            )

    async def test_spawn_plain_prompt(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._handle_agent_command = AsyncMock()  # type: ignore[method-assign]
            await app._handle_worktrees_command("/worktrees spawn refactor the parser")
            await pilot.pause()
            app._handle_agent_command.assert_awaited_once_with(
                "/agent spawn --worktree refactor the parser", echo=False
            )

    async def test_merge_resolves_task_branch(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._ensure_background_manager()
            task = MagicMock(status="completed", worktree_branch="agent/parser-1a2b3c")
            app._bg_manager.get_status = MagicMock(return_value=task)  # type: ignore[method-assign]
            app._handle_worktree_command = AsyncMock()  # type: ignore[method-assign]

            await app._handle_worktrees_command("/worktrees merge task-9")
            await pilot.pause()

            app._handle_worktree_command.assert_awaited_once_with(
                "/worktree merge agent/parser-1a2b3c", echo=False
            )


class TestWorktreesController:
    def test_parse_plain_prompt(self) -> None:
        from bog_agents_cli.worktrees_controller import parse_spawn_payload

        assert parse_spawn_payload("  fix the parser ") == (
            [{"label": "", "prompt": "fix the parser"}],
            None,
        )

    def test_parse_json_array_and_errors(self) -> None:
        from bog_agents_cli.worktrees_controller import USAGE, parse_spawn_payload

        items, error = parse_spawn_payload(
            '[{"label": "a", "prompt": "x"}, {"prompt": ""}, 3]'
        )
        assert error is None and items == [{"label": "a", "prompt": "x"}]
        assert parse_spawn_payload("[not json")[1].startswith("Invalid JSON")
        # A JSON object (not an array) is treated as plain prompt text.
        assert (
            parse_spawn_payload('{"prompt": "x"}')[0][0]["prompt"] == '{"prompt": "x"}'
        )
        assert parse_spawn_payload("")[1] == USAGE

    def test_render_tasks(self) -> None:
        from types import SimpleNamespace

        from bog_agents_cli.worktrees_controller import NO_TASKS, render_worktree_tasks

        plain = SimpleNamespace(
            status="running", worktree_branch=None, status_line=lambda: "t1 running"
        )
        wt = SimpleNamespace(
            status="running",
            worktree_branch="agent/x-1",
            status_line=lambda: "t2 running",
        )
        assert render_worktree_tasks([plain]) == NO_TASKS
        assert "agent/x-1" in render_worktree_tasks([plain, wt])
