"""Regression tests for the v5 Wave-A turn-lifecycle fixes.

Covers the audit findings:

* v5 CLIC-1 — `/telephone` ran inline on the App message pump and deadlocked
  the whole TUI waiting on a future only key events (dispatched by that same
  pump) could resolve.
* v5 CLIC-2/-5 — `/team run`, `/best-of-n`, `/jury`, and `/butcher` sessions
  ran either inline on the pump or in untracked workers, making them
  uninterruptible and invisible to TurnManager.
* v5 CLIC-4 — background-task completion notifications never fired
  (`call_from_thread` from the app's own thread always raises).
* v5 CLIC-6 — `_send_prompt_to_agent` gave callers no way to know whether
  their prompt was dispatched or deferred, so peat/pipeline waited on the
  wrong turn's worker handle.
* v5 CLIC-7 — `_resume_thread` could swap thread state under a live turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from bog_agents_cli.app import BogAgentsApp
from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage


def _messages_containing(app: BogAgentsApp, text: str) -> list[AppMessage]:
    return [w for w in app.query(AppMessage) if text in str(w._content)]


def _errors_containing(app: BogAgentsApp, text: str) -> list[ErrorMessage]:
    return [w for w in app.query(ErrorMessage) if text in str(w._content)]


async def _wait_for(condition, timeout: float = 5.0) -> bool:
    """Poll `condition()` until true or timeout; returns the final value."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return bool(condition())


class TestStartTrackedSession:
    """The single choke point for long-lived command sessions (v5 CLIC-2/-5)."""

    async def test_registers_with_turn_manager_and_esc_cancels(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            started = asyncio.Event()

            async def session() -> None:
                started.set()
                await asyncio.sleep(30)

            worker = app._start_tracked_session(session(), name="/test-session")
            assert app._agent_running is True
            assert app._agent_worker is worker

            await asyncio.wait_for(started.wait(), timeout=5)
            app.action_interrupt()
            assert await _wait_for(lambda: not app._agent_running)
            assert worker.is_cancelled
            assert app._agent_worker is None
            await pilot.pause()
            assert await _wait_for(
                lambda: _messages_containing(app, "/test-session interrupted.")
            )

    async def test_failure_mounts_error_and_ends_turn(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            async def session() -> None:
                msg = "kaboom"
                raise ValueError(msg)

            app._start_tracked_session(session(), name="/boom")
            assert await _wait_for(lambda: not app._agent_running)
            await pilot.pause()
            assert _errors_containing(app, "/boom failed: kaboom")
            assert app._agent_worker is None


class TestTelephoneOffPump:
    """v5 CLIC-1: /telephone must not block the App message pump."""

    async def test_menu_keys_resolve_while_pump_alive(self) -> None:
        from bog_agents_cli.widgets.telephone import TelephoneMenu

        app = BogAgentsApp()
        fake_model_result = MagicMock()

        async def fake_rewrite(prompt: str, model: object) -> str:
            return f"REWRITTEN: {prompt}"

        with (
            patch(
                "bog_agents_cli.config.create_model",
                return_value=fake_model_result,
            ),
            patch(
                "bog_agents_cli.telephone.rewrite_prompt_with_model",
                new=fake_rewrite,
            ),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                # The handler must return promptly (flow runs in a worker) —
                # the pre-fix code would deadlock right here.
                await asyncio.wait_for(
                    app._handle_telephone_command("/telephone make it faster"),
                    timeout=5,
                )
                assert await _wait_for(lambda: list(app.query(TelephoneMenu)))

                # Pump-liveness probe: a call_later callback must run while
                # the menu is up (the pre-fix pump was wedged here).
                probe = asyncio.Event()
                app.call_later(probe.set)
                await asyncio.wait_for(probe.wait(), timeout=2)

                # Ditch via the key path — dispatched through the pump, which
                # is exactly what the deadlock froze before the fix.
                await pilot.pause()
                await pilot.press("3")
                assert await _wait_for(
                    lambda: not list(app.query(TelephoneMenu))
                ), "telephone menu never resolved from its key binding"
                assert await _wait_for(
                    lambda: _messages_containing(app, "Discarded rewrite.")
                )


class TestButcherInterruptible:
    """v5 CLIC-5: butcher jobs must be tracked and cancellable."""

    async def test_esc_cancels_butcher_job(self) -> None:
        started = asyncio.Event()

        async def fake_job(app_: object, prompt: str) -> None:
            started.set()
            await asyncio.sleep(30)

        app = BogAgentsApp()
        with patch("bog_agents_cli.butcher.start_butcher_job", new=fake_job):
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._handle_butcher_command("/butcher do the thing")
                assert app._agent_running is True
                worker = app._agent_worker
                assert worker is not None

                await asyncio.wait_for(started.wait(), timeout=5)
                app.action_interrupt()
                assert await _wait_for(lambda: not app._agent_running)
                assert worker.is_cancelled
                await pilot.pause()
                assert await _wait_for(
                    lambda: _messages_containing(app, "/butcher interrupted.")
                )


class TestTeamAndBestOfNTracked:
    """v5 CLIC-2: /team run and /best-of-n run as tracked, cancellable sessions."""

    async def test_team_run_is_tracked_and_cancellable(self) -> None:
        started = asyncio.Event()

        async def fake_team(*args: object, **kwargs: object) -> object:
            started.set()
            await asyncio.sleep(30)
            return MagicMock()

        app = BogAgentsApp()
        with patch("bog_agents_cli.team_executor.run_team_session", new=fake_team):
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._handle_team_command("/team run do the thing")
                assert app._agent_running is True
                worker = app._agent_worker
                assert worker is not None

                await asyncio.wait_for(started.wait(), timeout=5)
                app.action_interrupt()
                assert await _wait_for(lambda: not app._agent_running)
                assert worker.is_cancelled
                await pilot.pause()
                assert await _wait_for(
                    lambda: _messages_containing(app, "/team run interrupted.")
                )

    async def test_team_run_refuses_while_busy(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._turns.begin_shell()
            try:
                await app._handle_team_command("/team run do the thing")
                await pilot.pause()
                assert _errors_containing(app, "Cannot start /team run")
            finally:
                app._turns.end_shell()

    async def test_best_of_n_refuses_while_busy(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._turns.begin_shell()
            try:
                await app._handle_best_of_n_command("/best-of-n 2 do the thing")
                await pilot.pause()
                assert _errors_containing(app, "Cannot start /best-of-n")
            finally:
                app._turns.end_shell()


class TestBackgroundCompletionNotification:
    """v5 CLIC-4: background task completion must mount a notification."""

    async def test_completion_notification_mounts(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._ensure_background_manager()

            async def runner(task: object) -> str:
                return "done result"

            task_id = await app._bg_manager.submit("do it", runner=runner)
            assert await _wait_for(
                lambda: _messages_containing(
                    app, f"Background task {task_id} completed."
                ),
                timeout=10,
            ), "completion notification never mounted (v5 CLIC-4 regression)"


class TestSendPromptDispatchContract:
    """v5 CLIC-6: callers must learn whether their prompt was dispatched."""

    async def test_returns_worker_when_idle(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent = MagicMock()
            app._ui_adapter = MagicMock()
            app._session_state = MagicMock()
            ran = asyncio.Event()

            async def fake_turn(prompt: str) -> None:
                ran.set()

            app._run_agent_task = fake_turn  # type: ignore[method-assign]
            worker = await app._send_prompt_to_agent("hello")
            assert worker is not None
            assert app._agent_worker is worker
            await asyncio.wait_for(ran.wait(), timeout=5)

    async def test_returns_none_and_queues_when_busy(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._turns.begin_shell()
            try:
                worker = await app._send_prompt_to_agent("hello")
                assert worker is None
                assert len(app._pending_messages) == 1
                assert app._pending_messages[0].text == "hello"
            finally:
                app._turns.end_shell()
                app._pending_messages.clear()


class TestResumeThreadBusyGuard:
    """v5 CLIC-7: a thread switch must not swap state under a live turn."""

    async def test_refuses_switch_while_turn_running(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._agent = MagicMock()
            session_state = MagicMock()
            session_state.thread_id = "thread-a"
            app._session_state = session_state
            app._turns.begin_shell()
            try:
                await app._resume_thread("thread-b")
                await pilot.pause()
                assert session_state.thread_id == "thread-a"
                assert _messages_containing(
                    app, "Cannot switch threads while a turn is running"
                )
            finally:
                app._turns.end_shell()
