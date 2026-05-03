"""Unit tests for bog_agents_cli.cmd_verify --no-output flag."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_verify import cmd_verify


class TestVerifyNoOutput:
    def _make_args(self, tmp_path: Path, *, no_output: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            cwd=str(tmp_path),
            output="",
            timeout=30,
            skip_typecheck=False,
            skip_lint=False,
            skip_test=False,
            output_format="text",
            no_output=no_output,
        )

    def _patch_profile(self, language: str = "python") -> MagicMock:
        mock_profile = MagicMock()
        mock_profile.language = language
        mock_profile.typecheck = "echo typecheck"
        mock_profile.lint = "echo lint"
        mock_profile.test = "echo test"
        return mock_profile

    def test_no_output_skips_file_write(self, tmp_path):
        args = self._make_args(tmp_path, no_output=True)
        mock_profile = self._patch_profile()
        with (
            patch("bog_agents_cli.cmd_verify.detect_project_profile", return_value=mock_profile),
            patch("bog_agents_cli.cmd_verify._run_check") as mock_run,
            patch("bog_agents_cli.cmd_verify._emit_text_report"),
            patch("bog_agents_cli.cmd_verify._format_summary", return_value="summary"),
        ):
            mock_run.return_value = MagicMock(exit_code=0, name="typecheck")
            result = cmd_verify(args)

        assert result == 0
        assert not (tmp_path / "verification_summary.md").exists()

    def test_default_writes_file(self, tmp_path):
        args = self._make_args(tmp_path, no_output=False)
        mock_profile = self._patch_profile()
        with (
            patch("bog_agents_cli.cmd_verify.detect_project_profile", return_value=mock_profile),
            patch("bog_agents_cli.cmd_verify._run_check") as mock_run,
            patch("bog_agents_cli.cmd_verify._emit_text_report"),
            patch("bog_agents_cli.cmd_verify._format_summary", return_value="summary content"),
        ):
            mock_run.return_value = MagicMock(exit_code=0, name="typecheck")
            result = cmd_verify(args)

        assert result == 0
        assert (tmp_path / "verification_summary.md").read_text() == "summary content"
