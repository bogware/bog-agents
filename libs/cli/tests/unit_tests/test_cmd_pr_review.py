"""Unit tests for bog_agents_cli.cmd_pr_review."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_pr_review import (
    build_pr_review_prompt,
    detect_pr_platform,
    format_pr_help,
    format_pr_review_not_found,
    get_github_pr_diff,
)

# ---------------------------------------------------------------------------
# detect_pr_platform
# ---------------------------------------------------------------------------


class TestDetectPrPlatform:
    def _mock_run(self, returncode=0, stdout=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_detects_github(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run",
            return_value=self._mock_run(stdout="https://github.com/org/repo.git\n"),
        ):
            result = detect_pr_platform(tmp_path)
        assert result == "github"

    def test_detects_azure_dev_azure(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run",
            return_value=self._mock_run(stdout="https://dev.azure.com/org/repo\n"),
        ):
            result = detect_pr_platform(tmp_path)
        assert result == "azure"

    def test_detects_azure_visualstudio(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run",
            return_value=self._mock_run(
                stdout="https://org.visualstudio.com/_git/repo\n"
            ),
        ):
            result = detect_pr_platform(tmp_path)
        assert result == "azure"

    def test_returns_none_for_unknown_remote(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run",
            return_value=self._mock_run(stdout="https://bitbucket.org/org/repo.git\n"),
        ):
            result = detect_pr_platform(tmp_path)
        assert result is None

    def test_returns_none_on_git_failure(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run",
            return_value=self._mock_run(returncode=1),
        ):
            result = detect_pr_platform(tmp_path)
        assert result is None

    def test_returns_none_when_git_not_found(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", side_effect=FileNotFoundError
        ):
            result = detect_pr_platform(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# get_github_pr_diff
# ---------------------------------------------------------------------------


class TestGetGithubPrDiff:
    def _meta_response(self, title="Test PR", number=42):
        return json.dumps(
            {
                "number": number,
                "title": title,
                "body": "PR description",
                "author": {"login": "testuser"},
                "url": "https://github.com/org/repo/pull/42",
            }
        )

    def _make_run_side_effect(
        self, meta_stdout, diff_stdout="diff --git a/foo.py b/foo.py\n"
    ):
        """Returns a side_effect that handles view then diff calls."""
        calls = []

        def _run(cmd, **_kwargs):
            calls.append(cmd)
            if "view" in cmd:
                return MagicMock(returncode=0, stdout=meta_stdout, stderr="")
            if "diff" in cmd:
                return MagicMock(returncode=0, stdout=diff_stdout, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return _run

    def test_raises_when_gh_not_found(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", side_effect=FileNotFoundError
        ):
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                get_github_pr_diff(cwd=tmp_path)

    def test_raises_when_not_authenticated(self, tmp_path):
        mock_result = MagicMock(returncode=1, stdout="", stderr="not logged in")
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", return_value=mock_result
        ):
            with pytest.raises(RuntimeError, match="not authenticated"):
                get_github_pr_diff(cwd=tmp_path)

    def test_returns_pr_metadata(self, tmp_path):
        side_effect = self._make_run_side_effect(self._meta_response())
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", side_effect=side_effect
        ):
            result = get_github_pr_diff(cwd=tmp_path)
        assert result["title"] == "Test PR"
        assert result["author"] == "testuser"
        assert "github.com" in result["url"]

    def test_returns_diff(self, tmp_path):
        diff = "diff --git a/foo.py b/foo.py\n+added line\n"
        side_effect = self._make_run_side_effect(self._meta_response(), diff)
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", side_effect=side_effect
        ):
            result = get_github_pr_diff(cwd=tmp_path)
        assert "added line" in result["diff"]

    def test_extracts_files_changed(self, tmp_path):
        diff = "diff --git a/foo.py b/foo.py\ndiff --git a/bar.ts b/bar.ts\n"
        side_effect = self._make_run_side_effect(self._meta_response(), diff)
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", side_effect=side_effect
        ):
            result = get_github_pr_diff(cwd=tmp_path)
        assert "foo.py" in result["files_changed"]
        assert "bar.ts" in result["files_changed"]

    def test_with_explicit_pr_number(self, tmp_path):
        side_effect = self._make_run_side_effect(self._meta_response())
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", side_effect=side_effect
        ) as mock_run:
            get_github_pr_diff(pr_number="99", cwd=tmp_path)
        # First call is view, should include PR number
        first_cmd = mock_run.call_args_list[0][0][0]
        assert "99" in first_cmd

    def test_raises_on_gh_pr_view_failure(self, tmp_path):
        mock_result = MagicMock(returncode=1, stdout="", stderr="no PR found")
        with patch(
            "bog_agents_cli.cmd_pr_review.subprocess.run", return_value=mock_result
        ):
            with pytest.raises(RuntimeError):
                get_github_pr_diff(cwd=tmp_path)


# ---------------------------------------------------------------------------
# build_pr_review_prompt
# ---------------------------------------------------------------------------


class TestBuildPrReviewPrompt:
    def _pr_data(self, **overrides):
        base = {
            "title": "My PR",
            "body": "Fixes a bug",
            "author": "alice",
            "diff": "diff --git a/foo.py b/foo.py\n+x = 1",
            "files_changed": "foo.py",
            "url": "https://github.com/org/repo/pull/1",
        }
        base.update(overrides)
        return base

    def test_includes_pr_title(self):
        result = build_pr_review_prompt(self._pr_data())
        assert "My PR" in result

    def test_includes_author(self):
        result = build_pr_review_prompt(self._pr_data())
        assert "alice" in result

    def test_includes_diff(self):
        result = build_pr_review_prompt(self._pr_data())
        assert "+x = 1" in result

    def test_includes_url(self):
        result = build_pr_review_prompt(self._pr_data())
        assert "https://github.com" in result

    def test_security_focus(self):
        result = build_pr_review_prompt(self._pr_data(), focus="security")
        assert "security" in result.lower()
        assert "injection" in result.lower()

    def test_performance_focus(self):
        result = build_pr_review_prompt(self._pr_data(), focus="performance")
        assert "performance" in result.lower()

    def test_all_focus_default(self):
        result = build_pr_review_prompt(self._pr_data())
        assert "Logic errors" in result or "logic" in result.lower()

    def test_includes_files_changed(self):
        result = build_pr_review_prompt(self._pr_data(files_changed="foo.py, bar.ts"))
        assert "foo.py" in result

    def test_includes_pr_description(self):
        result = build_pr_review_prompt(self._pr_data(body="Fixes issue #123"))
        assert "Fixes issue #123" in result

    def test_no_diff_available_message(self):
        result = build_pr_review_prompt(self._pr_data(diff=""))
        assert "no diff available" in result


# ---------------------------------------------------------------------------
# format_pr_review_not_found
# ---------------------------------------------------------------------------


class TestFormatPrReviewNotFound:
    def test_github_message(self):
        result = format_pr_review_not_found("github")
        assert "GitHub" in result
        assert "gh auth login" in result

    def test_azure_message(self):
        result = format_pr_review_not_found("azure")
        assert "Azure" in result

    def test_unknown_platform(self):
        result = format_pr_review_not_found(None)
        assert "No PR found" in result

    def test_returns_string(self):
        assert isinstance(format_pr_review_not_found("github"), str)


# ---------------------------------------------------------------------------
# format_pr_help
# ---------------------------------------------------------------------------


class TestFormatPrHelp:
    def test_returns_string(self):
        result = format_pr_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_pr_review(self):
        result = format_pr_help()
        assert "/pr review" in result

    def test_mentions_platforms(self):
        result = format_pr_help()
        assert "GitHub" in result
        assert "Azure" in result
