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
