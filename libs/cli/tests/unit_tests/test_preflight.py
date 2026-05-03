"""Unit tests for bog_agents_cli.preflight."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bog_agents_cli.auto_mode import AutoModeSettings, HaikuEvalConfig
from bog_agents_cli.preflight import run_preflight_qa


class TestRunPreflightQa:
    @pytest.fixture
    def settings(self) -> AutoModeSettings:
        return AutoModeSettings(
            enabled=True,
            preflight_clarification=True,
            haiku_eval=HaikuEvalConfig(enabled=False),  # avoid network in most tests
        )

    async def test_returns_prompt_when_disabled(self, settings):
        settings.preflight_clarification = False
        out = await run_preflight_qa("fix everything", settings=settings)
        assert out == "fix everything"

    async def test_returns_prompt_when_quiet(self, settings):
        out = await run_preflight_qa("fix everything", settings=settings, quiet=True)
        assert out == "fix everything"

    async def test_returns_prompt_when_stdin_not_tty(self, settings):
        with patch("bog_agents_cli.preflight.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            out = await run_preflight_qa("fix everything", settings=settings)
        assert out == "fix everything"

    async def test_returns_prompt_when_no_ambiguity(self, settings):
        with patch("bog_agents_cli.preflight.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            out = await run_preflight_qa(
                "Add type hints to the parse_url function in url_utils.py",
                settings=settings,
            )
        assert out.startswith("Add type hints")

    async def test_eof_skips_remaining(self, settings):
        with (
            patch("bog_agents_cli.preflight.sys.stdin") as mock_stdin,
            patch("bog_agents_cli.preflight.asyncio.to_thread", side_effect=EOFError),
        ):
            mock_stdin.isatty.return_value = True
            out = await run_preflight_qa("fix everything", settings=settings)
        # Ambiguity matched but EOF on first answer → prompt unchanged
        assert out == "fix everything"

    async def test_collects_answers(self, settings):
        with (
            patch("bog_agents_cli.preflight.sys.stdin") as mock_stdin,
            patch("bog_agents_cli.preflight.asyncio.to_thread", return_value="the login bug"),
        ):
            mock_stdin.isatty.return_value = True
            out = await run_preflight_qa("fix everything", settings=settings)
        assert "Pre-flight context:" in out
        assert "the login bug" in out

    async def test_timeout_breaks_loop(self, settings):
        # asyncio.wait_for raising TimeoutError should bail out of further
        # questions without crashing the run.
        with (
            patch("bog_agents_cli.preflight.sys.stdin") as mock_stdin,
            patch("bog_agents_cli.preflight.asyncio.wait_for", side_effect=TimeoutError),
        ):
            mock_stdin.isatty.return_value = True
            out = await run_preflight_qa("fix everything", settings=settings)
        assert out == "fix everything"

    async def test_haiku_unavailable_does_not_crash(self, settings):
        # Even when haiku_eval is enabled, an ImportError or API error in
        # haiku_preflight_check should be swallowed by the helper and the
        # heuristic results used.
        settings.haiku_eval.enabled = True
        with (
            patch("bog_agents_cli.preflight.sys.stdin") as mock_stdin,
            patch("bog_agents_cli.preflight.haiku_preflight_check", return_value=[]),
            patch("bog_agents_cli.preflight.asyncio.to_thread", return_value=""),
        ):
            mock_stdin.isatty.return_value = True
            out = await run_preflight_qa("fix everything", settings=settings)
        # No answers given → original prompt returned
        assert out == "fix everything"
