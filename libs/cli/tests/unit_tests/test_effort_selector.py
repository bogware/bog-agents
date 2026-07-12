"""Tests for the reasoning-effort picker modal (`/effort` with no argument)."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from bog_agents_cli.reasoning_effort import (
    ANTHROPIC_EFFORTS,
    LEGACY_EFFORT_LEVELS,
)
from bog_agents_cli.widgets.effort_selector import (
    EffortOption,
    EffortSelectorScreen,
)

_REASONING_MODEL = "anthropic:claude-opus-4-8"
_NON_REASONING_MODEL = "openai:gpt-4o"


class EffortSelectorTestApp(App[None]):
    """Minimal app wrapper for testing EffortSelectorScreen."""

    def compose(self) -> ComposeResult:
        yield Static("base")


def _option_levels(screen: EffortSelectorScreen) -> list[str]:
    """Return the effort levels rendered as rows, in order."""
    return [opt.effort_level for opt in screen.query(EffortOption)]


class TestEffortSelectorLevels:
    """The picker offers exactly the levels valid for the active model."""

    async def test_reasoning_model_lists_native_levels(self) -> None:
        """A reasoning model shows its native supported effort set."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            screen = EffortSelectorScreen(model_spec=_REASONING_MODEL, current="high")
            app.push_screen(screen)
            await pilot.pause()

            assert _option_levels(screen) == list(ANTHROPIC_EFFORTS)
            assert screen._native is True
            assert screen._default == "high"

    async def test_non_reasoning_model_lists_legacy_levels(self) -> None:
        """A non-reasoning model falls back to the legacy preset vocabulary."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            screen = EffortSelectorScreen(
                model_spec=_NON_REASONING_MODEL, current="medium"
            )
            app.push_screen(screen)
            await pilot.pause()

            assert _option_levels(screen) == list(LEGACY_EFFORT_LEVELS)
            assert screen._native is False
            # No documented provider default for a non-reasoning model.
            assert screen._default is None
            # xhigh is a reasoning-only level and must not appear here.
            assert "xhigh" not in _option_levels(screen)

    async def test_current_and_default_are_marked(self) -> None:
        """The current level and provider default get inline markers."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            screen = EffortSelectorScreen(model_spec=_REASONING_MODEL, current="low")
            app.push_screen(screen)
            await pilot.pause()

            rows = {opt.effort_level: opt for opt in screen.query(EffortOption)}
            low_label = str(rows["low"]._Static__content)  # type: ignore[attr-defined]
            high_label = str(rows["high"]._Static__content)  # type: ignore[attr-defined]
            assert "current" in low_label
            assert "default" in high_label

    async def test_current_level_is_preselected(self) -> None:
        """The highlighted row starts on the currently active level."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            screen = EffortSelectorScreen(model_spec=_REASONING_MODEL, current="xhigh")
            app.push_screen(screen)
            await pilot.pause()

            assert screen._levels[screen._selected_index] == "xhigh"


class TestEffortSelectorInteraction:
    """Navigation and apply/cancel behavior."""

    async def test_enter_applies_highlighted_level(self) -> None:
        """Enter dismisses with the highlighted level."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            result: list[str | None] = []

            def on_dismiss(value: str | None) -> None:
                result.append(value)

            screen = EffortSelectorScreen(model_spec=_REASONING_MODEL, current="low")
            app.push_screen(screen, on_dismiss)
            await pilot.pause()

            await pilot.press("down")  # low -> medium
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert result == ["medium"]

    async def test_escape_cancels_with_none(self) -> None:
        """Escape dismisses without a choice."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            result: list[str | None] = []

            def on_dismiss(value: str | None) -> None:
                result.append(value)

            screen = EffortSelectorScreen(model_spec=_REASONING_MODEL, current="low")
            app.push_screen(screen, on_dismiss)
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()

            assert result == [None]

    async def test_navigation_wraps(self) -> None:
        """Up from the first row wraps to the last."""
        app = EffortSelectorTestApp()
        async with app.run_test() as pilot:
            screen = EffortSelectorScreen(model_spec=_REASONING_MODEL, current="low")
            app.push_screen(screen)
            await pilot.pause()

            assert screen._selected_index == 0
            await pilot.press("up")
            await pilot.pause()
            assert screen._selected_index == len(screen._levels) - 1
