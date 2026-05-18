"""Tests for /expert watch — scheduled proposer (Wave I)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bog_agents_cli import expert_watch


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset watcher registry and bypass the production interval floor.

    Production keeps a 60s floor (no point firing the proposer more
    than once per minute). Tests need sub-second intervals to keep the
    suite fast — we lift the floor by overriding the module's
    ``_min_interval_override`` global, which the real start() honors.
    """
    monkeypatch.setattr(expert_watch, "_min_interval_override", 0.01)
    expert_watch.reset()


# ---------------------------------------------------------------------------
# Status / is_running
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_when_not_running(self, tmp_path: Path) -> None:
        text = expert_watch.status(tmp_path)
        assert "not running" in text
        assert "Start with /expert watch start" in text
        assert expert_watch.is_running(tmp_path) is False


# ---------------------------------------------------------------------------
# Start / stop happy path
# ---------------------------------------------------------------------------


async def test_start_and_stop(tmp_path: Path) -> None:
    fired: list = []

    def propose(_agent_id: str, *, auto_activate: bool = False) -> str:
        fired.append(auto_activate)
        return "Saved proposal: gate.yaml"

    started, msg = expert_watch.start(
        working_dir=tmp_path,
        propose=propose,
        interval_seconds=60,  # gets clamped at floor anyway
    )
    assert started
    assert "Started expert watcher" in msg
    assert expert_watch.is_running(tmp_path)

    status_text = expert_watch.status(tmp_path)
    assert "RUNNING" in status_text
    assert "STAGED" in status_text

    stopped, stop_msg = await expert_watch.stop(tmp_path)
    assert stopped
    assert "Stopped" in stop_msg
    assert not expert_watch.is_running(tmp_path)


# ---------------------------------------------------------------------------
# Double-start
# ---------------------------------------------------------------------------


def _dummy_propose_x(_a: str, *, auto_activate: bool = False) -> str:
    return "Saved proposal: x.yaml"


def _dummy_propose_y(_a: str, *, auto_activate: bool = False) -> str:
    return "Saved proposal: y.yaml"


async def test_double_start_refused(tmp_path: Path) -> None:
    expert_watch.start(
        working_dir=tmp_path,
        propose=_dummy_propose_x,
        interval_seconds=60,
    )
    started, msg = expert_watch.start(
        working_dir=tmp_path,
        propose=_dummy_propose_y,
        interval_seconds=60,
    )
    assert not started
    assert "already running" in msg
    await expert_watch.stop(tmp_path)


# ---------------------------------------------------------------------------
# Stop when nothing running
# ---------------------------------------------------------------------------


async def test_stop_when_nothing_running(tmp_path: Path) -> None:
    stopped, msg = await expert_watch.stop(tmp_path)
    assert not stopped
    assert "No expert watcher" in msg


# ---------------------------------------------------------------------------
# Auto-activate flag flows through
# ---------------------------------------------------------------------------


async def test_auto_activate_flag(tmp_path: Path) -> None:
    received: list[bool] = []

    def propose(_agent_id: str, *, auto_activate: bool = False) -> str:
        received.append(auto_activate)
        return "Auto-activated rule: x.yaml"

    expert_watch.start(
        working_dir=tmp_path,
        propose=propose,
        interval_seconds=0.05,
        auto_activate=True,
    )
    # Sleep just long enough for the loop to wake up once.
    await asyncio.sleep(0.15)
    await expert_watch.stop(tmp_path)
    assert received, "propose should have been called at least once"
    assert all(received), "auto_activate=True should flow through"


# ---------------------------------------------------------------------------
# Per-run callback fires
# ---------------------------------------------------------------------------


async def test_on_summary_callback_fires(tmp_path: Path) -> None:
    summaries: list[str] = []

    async def on_summary(text: str) -> None:
        summaries.append(text)

    def propose(_a: str, *, auto_activate: bool = False) -> str:
        return "Saved proposal: r.yaml"

    expert_watch.start(
        working_dir=tmp_path,
        propose=propose,
        interval_seconds=0.05,
        on_summary=on_summary,
    )
    await asyncio.sleep(0.18)
    await expert_watch.stop(tmp_path)
    assert summaries
    assert any("Saved proposal" in s for s in summaries)


# ---------------------------------------------------------------------------
# Propose raising doesn't crash the loop
# ---------------------------------------------------------------------------


async def test_propose_exception_doesnt_kill_loop(tmp_path: Path) -> None:
    calls = {"n": 0}

    def propose(_a: str, *, auto_activate: bool = False) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "first call boom"
            raise RuntimeError(msg)
        return "Saved proposal: ok.yaml"

    expert_watch.start(
        working_dir=tmp_path,
        propose=propose,
        interval_seconds=0.05,
    )
    await asyncio.sleep(0.3)
    await expert_watch.stop(tmp_path)
    # The loop should have survived the first call's exception and
    # made at least one more call afterward.
    assert calls["n"] >= 2, f"loop should have survived first crash; calls={calls}"


# ---------------------------------------------------------------------------
# Controller dispatcher routes the subcommands
# ---------------------------------------------------------------------------


class TestControllerDispatch:
    def test_status_via_dispatch(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import (
            dispatch,
            reset_controllers,
        )

        reset_controllers()
        out = dispatch("/expert watch", tmp_path)
        assert "not running" in out

    def test_unknown_subcommand(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import (
            dispatch,
            reset_controllers,
        )

        reset_controllers()
        out = dispatch("/expert watch wibble", tmp_path)
        assert "Usage" in out

    def test_invalid_interval_rejected(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import (
            dispatch,
            reset_controllers,
        )

        reset_controllers()
        out = dispatch("/expert watch start notanumber", tmp_path)
        assert "Invalid interval" in out


# ---------------------------------------------------------------------------
# Slash spec carries the watch subcommands
# ---------------------------------------------------------------------------


class TestSlashSpec:
    def test_watch_subcommands_advertised(self) -> None:
        from bog_agents_cli.commands import general

        expert_cmd = next(c for c in general.COMMANDS if c.name == "/expert")
        subs = {s[0] for s in expert_cmd.spec.subcommands}
        assert "watch" in subs
        assert "watch start [N] [--apply]" in subs
        assert "watch stop" in subs


# ---------------------------------------------------------------------------
# Wave J1: controller-supplied watcher callback
# ---------------------------------------------------------------------------


class TestControllerCallback:
    """set_watch_summary_callback should flow into expert_watch.start."""

    async def test_callback_fires_when_set_via_controller(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import (
            get_controller,
            reset_controllers,
        )

        reset_controllers()
        seen: list[str] = []

        async def cb(summary: str) -> None:
            seen.append(summary)

        c = get_controller(tmp_path)
        c.set_watch_summary_callback(cb)
        # Stub the controller's propose so the loop has something fast
        # to call.
        c.propose_from_dreamscape = lambda _a, *, auto_activate=False: (
            "Saved proposal: x.yaml"
        )

        out = c._dispatch_watch_start("0.05")
        assert "Started" in out
        await asyncio.sleep(0.18)
        from bog_agents_cli import expert_watch as _w

        await _w.stop(tmp_path)
        assert seen, "callback should have been invoked at least once"
        assert any("Saved proposal" in s for s in seen)

    def test_callback_can_be_cleared(self, tmp_path: Path) -> None:
        from bog_agents_cli.expert_controller import (
            get_controller,
            reset_controllers,
        )

        reset_controllers()
        c = get_controller(tmp_path)

        async def cb(_: str) -> None:
            pass

        c.set_watch_summary_callback(cb)
        assert c._on_watch_summary is cb
        c.set_watch_summary_callback(None)
        assert c._on_watch_summary is None


# ---------------------------------------------------------------------------
# K2: persistence across app restart
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_creates_file(self, tmp_path: Path) -> None:
        path = expert_watch.save_state(
            tmp_path, interval_seconds=120, auto_activate=False, agent_id="x"
        )
        assert path.is_file()
        assert path.parent.name == ".bog-agents"
        assert path.name == "watch-state.toml"

    def test_load_round_trips(self, tmp_path: Path) -> None:
        expert_watch.save_state(
            tmp_path, interval_seconds=200, auto_activate=True, agent_id="alpha"
        )
        state = expert_watch.load_state(tmp_path)
        assert state is not None
        assert state["interval_seconds"] == 200
        assert state["auto_activate"] is True
        assert state["agent_id"] == "alpha"
        assert "started_at" in state

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert expert_watch.load_state(tmp_path) is None

    def test_clear_state(self, tmp_path: Path) -> None:
        expert_watch.save_state(
            tmp_path, interval_seconds=120, auto_activate=False, agent_id="x"
        )
        assert expert_watch.clear_state(tmp_path) is True
        assert expert_watch.load_state(tmp_path) is None
        # Idempotent: second clear returns False.
        assert expert_watch.clear_state(tmp_path) is False


async def test_start_persists_and_stop_clears(tmp_path: Path) -> None:
    expert_watch.start(
        working_dir=tmp_path,
        propose=_dummy_propose_x,
        interval_seconds=60,
    )
    state = expert_watch.load_state(tmp_path)
    assert state is not None
    assert state["interval_seconds"] >= 0.01  # honor the test floor override
    stopped, _ = await expert_watch.stop(tmp_path)
    assert stopped
    assert expert_watch.load_state(tmp_path) is None


async def test_resume_if_persisted_starts_a_watcher(tmp_path: Path) -> None:
    expert_watch.save_state(
        tmp_path,
        interval_seconds=0.05,
        auto_activate=False,
        agent_id="resume-test",
    )
    seen_summaries: list[str] = []

    async def _on_summary(text: str) -> None:
        seen_summaries.append(text)

    resumed, message = expert_watch.resume_if_persisted(
        working_dir=tmp_path,
        propose=_dummy_propose_x,
        on_summary=_on_summary,
    )
    assert resumed
    assert "Started" in message
    assert expert_watch.is_running(tmp_path)
    await asyncio.sleep(0.15)
    await expert_watch.stop(tmp_path)
    assert seen_summaries, "resumed watcher should have fired on_summary"


def test_resume_without_state_is_noop(tmp_path: Path) -> None:
    resumed, message = expert_watch.resume_if_persisted(
        working_dir=tmp_path,
        propose=_dummy_propose_x,
    )
    assert not resumed
    assert "no persisted" in message.lower()


def test_resume_handles_malformed_state(tmp_path: Path) -> None:
    state_dir = tmp_path / ".bog-agents"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "watch-state.toml").write_text(
        "not = ::: valid toml\n[", encoding="utf-8"
    )
    resumed, _ = expert_watch.resume_if_persisted(
        working_dir=tmp_path,
        propose=_dummy_propose_x,
    )
    assert not resumed
