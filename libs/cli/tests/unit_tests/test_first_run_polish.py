"""Wave B first-30-minutes polish (v6 CLI-5 / CLI-6 / CLI-7 / CLI-13)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from bog_agents_cli.app import BogAgentsApp
from bog_agents_cli.widgets.messages import AppMessage, DiffMessage

_UNIFIED = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-print('a [old]')\n+print('b [new]')\n"


class TestDiffCommand:
    async def test_unified_diff_renders_as_coloured_diff_widget(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._run_git = AsyncMock(return_value=(True, _UNIFIED))  # type: ignore[method-assign]

            await app._handle_diff_command("/diff")
            await pilot.pause()

            diffs = list(app.query(DiffMessage))
            assert len(diffs) == 1
            assert diffs[0]._diff_content == _UNIFIED
            assert diffs[0]._max_lines == 600

    async def test_stat_stays_plain_text_with_markup_escaped(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._run_git = AsyncMock(
                return_value=(True, " x.py | 2 +-\n 1 file changed\n")
            )  # type: ignore[method-assign]

            await app._handle_diff_command("/diff --stat")
            await pilot.pause()

            assert not list(app.query(DiffMessage))
            assert any(
                "1 file changed" in str(w._content) for w in app.query(AppMessage)
            )

    async def test_no_changes_message(self) -> None:
        app = BogAgentsApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._run_git = AsyncMock(return_value=(True, ""))  # type: ignore[method-assign]
            await app._handle_diff_command("/diff")
            await pilot.pause()
            assert any(
                "No pending git changes" in str(w._content)
                for w in app.query(AppMessage)
            )


class TestCostAwareDefaults:
    def test_anthropic_auto_default_is_sonnet_class(self) -> None:
        from bog_agents_cli.provider_catalog import DEFAULT_MODEL_CANDIDATES

        first = DEFAULT_MODEL_CANDIDATES["anthropic"][0]
        assert "sonnet" in first
        assert "opus" in DEFAULT_MODEL_CANDIDATES["anthropic"][1]

    def test_price_hint_for_priced_and_unpriced_specs(self) -> None:
        from bog_agents_cli.config import price_hint_for_spec

        hint = price_hint_for_spec("anthropic:claude-sonnet-4-6")
        assert hint.startswith("≈ $") and "per 1M tokens" in hint and "/cost" in hint
        assert price_hint_for_spec("ollama:definitely-not-a-priced-model") == ""

    def test_retired_bedrock_id_is_gone(self) -> None:
        from pathlib import Path

        import bog_agents_cli.config as cfg
        from bog_agents_cli import main

        for module in (cfg, main):
            assert "claude-sonnet-4-20250514" not in Path(module.__file__).read_text(
                encoding="utf-8"
            )
