"""Tests for ``bog_agents_cli.race``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import tomli_w

from bog_agents_cli.race import (
    Racer,
    RaceResult,
    load_race_specs,
    pick_winner,
    run_race,
)


def _model(text: str) -> object:
    m = MagicMock()
    response = MagicMock()
    response.content = text
    m.ainvoke = AsyncMock(return_value=response)
    return m


def test_load_race_specs_empty_when_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "missing.toml"
    assert load_race_specs(cfg) == []


def test_load_race_specs_reads_list(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    with cfg.open("wb") as f:
        tomli_w.dump({"race": {"models": ["a:1", "b:2"]}}, f)
    assert load_race_specs(cfg) == ["a:1", "b:2"]


async def test_run_race_collects_all_results() -> None:
    racers = [
        Racer("alpha", _model("hello from alpha")),
        Racer("beta", _model("longer response from beta with extra fluff")),
    ]
    report = await run_race("do the thing", racers)
    assert len(report.results) == 2
    assert {r.label for r in report.results} == {"alpha", "beta"}


async def test_run_race_passes_system_prompt_when_set() -> None:
    captured: list[list[object]] = []

    def make_capturing_model() -> object:
        m = MagicMock()
        response = MagicMock()
        response.content = "x"

        async def ainvoke(messages: list[object]) -> object:  # noqa: RUF029  # async generator
            captured.append(list(messages))
            return response

        m.ainvoke = ainvoke
        return m

    racers = [
        Racer("a", make_capturing_model(), system_prompt="be terse"),
        Racer("b", make_capturing_model()),
    ]
    await run_race("hello", racers)
    assert len(captured[0]) == 2  # system + user
    assert len(captured[1]) == 1  # user only


async def test_run_race_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await run_race("", [])


async def test_run_race_rejects_empty_racers() -> None:
    with pytest.raises(ValueError, match="at least one racer"):
        await run_race("hi", [])


def test_pick_winner_returns_shortest_successful() -> None:
    results = (
        RaceResult("a", "this is a long response with extra fluff", 1.0),
        RaceResult("b", "short", 1.5),
        RaceResult("c", "", 0.5, error="boom"),
    )
    from bog_agents_cli.race import RaceReport

    report = RaceReport(prompt="x", results=results)
    winner = pick_winner(report)
    assert winner is not None
    assert winner.label == "b"


def test_pick_winner_returns_none_when_all_failed() -> None:
    from bog_agents_cli.race import RaceReport

    report = RaceReport(
        prompt="x",
        results=(RaceResult("a", "", 0.0, error="boom"),),
    )
    assert pick_winner(report) is None


def test_race_command_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/race" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/race"] == "_handle_race_command"
