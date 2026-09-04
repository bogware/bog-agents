"""`/checkpoint load` restores the saved thread (v6 CLI-2 = v4 P1-27).

The handler used to resolve the checkpoint's thread id and then send the
model a prose "resume from checkpoint" prompt in the *current* thread, so
nothing was restored and the model confabulated a resume.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from bog_agents_cli.app import BogAgentsApp
from bog_agents_cli.widgets.messages import AppMessage


def _messages_containing(app: BogAgentsApp, text: str) -> list[AppMessage]:
    return [w for w in app.query(AppMessage) if text in str(w._content)]


async def test_checkpoint_load_switches_thread_instead_of_prompting() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._resume_thread = AsyncMock()  # type: ignore[method-assign]
        app._send_prompt_to_agent = AsyncMock()  # type: ignore[method-assign]

        with patch(
            "bog_agents_cli.cmd_checkpoint.load_checkpoint", return_value="thread-42"
        ):
            await app._handle_checkpoint_command("/checkpoint load before-refactor")
            await pilot.pause()

        app._resume_thread.assert_awaited_once_with("thread-42")
        app._send_prompt_to_agent.assert_not_awaited()
        assert _messages_containing(app, "Restoring checkpoint 'before-refactor'")


async def test_checkpoint_load_unknown_name_reports_and_does_nothing() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._resume_thread = AsyncMock()  # type: ignore[method-assign]

        with patch("bog_agents_cli.cmd_checkpoint.load_checkpoint", return_value=None):
            await app._handle_checkpoint_command("/checkpoint restore nope")
            await pilot.pause()

        app._resume_thread.assert_not_awaited()
        assert _messages_containing(app, "No checkpoint named 'nope'")
