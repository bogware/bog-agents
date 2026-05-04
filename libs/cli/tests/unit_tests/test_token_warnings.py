"""Tests for the 70/80/90% context-utilization warning hook."""

from __future__ import annotations

from bog_agents_cli.app import TextualTokenTracker


def test_no_warn_when_no_callback() -> None:
    tracker = TextualTokenTracker(lambda _v: None, context_window=1000)
    tracker.add(950)  # would be 95%
    # No callback configured — nothing to assert beyond not raising.
    assert tracker.current_context == 950


def test_no_warn_when_no_window() -> None:
    fires: list[tuple[int, int]] = []
    tracker = TextualTokenTracker(
        lambda _v: None,
        context_window=0,
        warn_callback=lambda pct, t: fires.append((pct, t)),
    )
    tracker.add(1_000_000)
    assert fires == []


def test_fires_70_then_80_then_90() -> None:
    fires: list[tuple[int, int]] = []
    tracker = TextualTokenTracker(
        lambda _v: None,
        context_window=1000,
        warn_callback=lambda pct, t: fires.append((pct, t)),
    )
    tracker.add(700)
    tracker.add(800)
    tracker.add(905)
    assert [t for _, t in fires] == [70, 80, 90]


def test_does_not_refire_until_drop_below() -> None:
    fires: list[tuple[int, int]] = []
    tracker = TextualTokenTracker(
        lambda _v: None,
        context_window=1000,
        warn_callback=lambda pct, t: fires.append((pct, t)),
    )
    tracker.add(720)  # crosses 70 → fires
    tracker.add(740)  # still ≥70 — no re-fire
    assert len(fires) == 1
    # Drop below 70, then come back — should re-fire.
    tracker.add(500)
    tracker.add(720)
    assert len(fires) == 2


def test_set_context_window_resets_fired_set() -> None:
    fires: list[tuple[int, int]] = []
    tracker = TextualTokenTracker(
        lambda _v: None,
        context_window=1000,
        warn_callback=lambda pct, t: fires.append((pct, t)),
    )
    tracker.add(720)
    tracker.set_context_window(500)
    tracker.add(360)  # 72% of 500 — should fire again because we re-armed.
    assert len(fires) == 2


def test_reset_clears_fired_thresholds() -> None:
    fires: list[tuple[int, int]] = []
    tracker = TextualTokenTracker(
        lambda _v: None,
        context_window=1000,
        warn_callback=lambda pct, t: fires.append((pct, t)),
    )
    tracker.add(720)
    tracker.reset()
    tracker.add(720)
    assert len(fires) == 2


def test_one_warning_per_add_call() -> None:
    """Even if a single update jumps from 50% to 95%, only one warn fires."""
    fires: list[tuple[int, int]] = []
    tracker = TextualTokenTracker(
        lambda _v: None,
        context_window=1000,
        warn_callback=lambda pct, t: fires.append((pct, t)),
    )
    tracker.add(950)
    assert len(fires) == 1
