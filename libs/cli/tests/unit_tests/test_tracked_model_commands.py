"""Every model-calling slash command runs as a tracked session (v6 CLI-3).

v5 Wave A routed `/butcher`, `/team run`, `/best-of-n` and `/jury` through
`_start_tracked_session`; these nine still awaited their model calls inline on
the App message pump, so Esc did nothing for minutes and a prompt typed
meanwhile started a concurrent turn against the same files. Each test blocks
the command's model-bound callable, checks TurnManager sees the session, then
interrupts it and expects the "<name> interrupted." toast.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bog_agents_cli.app import BogAgentsApp
from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage


def _messages_containing(app: BogAgentsApp, text: str) -> list[AppMessage]:
    return [w for w in app.query(AppMessage) if text in str(w._content)]


def _errors_containing(app: BogAgentsApp, text: str) -> list[ErrorMessage]:
    return [w for w in app.query(ErrorMessage) if text in str(w._content)]


async def _wait_for(condition, timeout_s: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return bool(condition())


class _Blocker:
    """An awaitable / callable that parks until released, recording the start."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = threading.Event()

    async def async_block(self, *_a: object, **_k: object) -> object:
        self.started.set()
        await asyncio.sleep(30)
        return SimpleNamespace()

    def sync_block(self, *_a: object, **_k: object) -> object:
        self.started.set()
        self.release.wait(10)
        return SimpleNamespace()


async def _assert_tracked_and_interruptible(
    app: BogAgentsApp, pilot, blocker: _Blocker, name: str
) -> None:
    await asyncio.wait_for(blocker.started.wait(), timeout=5)
    assert app._turns.busy, f"{name} did not register with TurnManager"
    assert app._agent_running
    app.action_interrupt()
    blocker.release.set()
    assert await _wait_for(lambda: not app._agent_running)
    await pilot.pause()
    assert await _wait_for(
        lambda: bool(_messages_containing(app, f"{name} interrupted."))
    )


async def test_orchestrate_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with patch(
            "bog_agents_cli.orchestrator_controller.OrchestratorController.run",
            b.sync_block,
        ):
            await app._handle_orchestrate_command(
                "/orchestrate refactor the auth module"
            )
            await _assert_tracked_and_interruptible(app, pilot, b, "/orchestrate")


async def test_sidecar_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with patch(
            "bog_agents_cli.sidecar_controller.SidecarController.run", b.sync_block
        ):
            await app._handle_sidecar_command("/sidecar why did the test fail?")
            await _assert_tracked_and_interruptible(app, pilot, b, "/sidecar")


async def test_race_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with (
            patch("bog_agents_cli.race.load_race_specs", return_value=["fake:model"]),
            patch(
                "bog_agents_cli.config.create_model",
                return_value=SimpleNamespace(model=object()),
            ),
            patch("bog_agents_cli.race.run_race", b.async_block),
        ):
            await app._handle_race_command("/race implement the parser")
            await _assert_tracked_and_interruptible(app, pilot, b, "/race")


async def test_imagine_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with patch("bog_agents_cli.imagine.run_imagine", b.async_block):
            await app._handle_imagine_command("/imagine 3 caching strategies")
            await _assert_tracked_and_interruptible(app, pilot, b, "/imagine")


async def test_devil_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with patch("bog_agents_cli.devil.run_devil", b.async_block):
            await app._handle_devil_command("/devil")
            await _assert_tracked_and_interruptible(app, pilot, b, "/devil")


async def test_handoff_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with patch("bog_agents_cli.handoff.run_handoff", b.async_block):
            await app._handle_handoff_command("/handoff")
            await _assert_tracked_and_interruptible(app, pilot, b, "/handoff")


async def test_squad_review_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        with patch("bog_agents_cli.squad.run_squad", b.async_block):
            await app._handle_squad_command("/squad review the diff")
            await _assert_tracked_and_interruptible(app, pilot, b, "/squad")


async def test_rubric_draft_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        record = SimpleNamespace(is_set=True, objective="ship the parser", rubric=[])
        with (
            patch("bog_agents_cli.goal_controller.load_goal", return_value=record),
            patch(
                "bog_agents_cli.goal_rubric.build_invoke",
                return_value=lambda *a, **k: "",
            ),
            patch("bog_agents_cli.goal_rubric.draft_criteria", b.async_block),
        ):
            await app._handle_rubric_command("/rubric draft")
            await _assert_tracked_and_interruptible(app, pilot, b, "/rubric draft")


async def test_teach_is_tracked() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        b = _Blocker()
        app._lc_thread_id = "thread-1"
        app._get_thread_state_values = AsyncMock(  # type: ignore[method-assign]
            return_value={"messages": [SimpleNamespace(type="human", content="hello")]}
        )
        with (
            patch(
                "bog_agents_cli.config.create_model",
                return_value=SimpleNamespace(model=object()),
            ),
            patch(
                "bog_agents_cli.skill_flywheel.propose_skills_from_transcript",
                b.async_block,
            ),
        ):
            await app._handle_teach_command("/teach")
            await _assert_tracked_and_interruptible(app, pilot, b, "/teach")


async def test_model_command_refused_while_session_in_flight() -> None:
    app = BogAgentsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        started = asyncio.Event()

        async def session() -> None:
            started.set()
            await asyncio.sleep(30)

        app._start_tracked_session(session(), name="/other")
        await asyncio.wait_for(started.wait(), timeout=5)

        with patch("bog_agents_cli.devil.run_devil", AsyncMock()) as run_devil:
            await app._handle_devil_command("/devil")
            await pilot.pause()
            run_devil.assert_not_awaited()
        assert _errors_containing(
            app, "Cannot start /devil while another turn or session is in flight."
        )
        app.action_interrupt()
        assert await _wait_for(lambda: not app._agent_running)
