"""Unit tests for bog_agents_cli.cmd_memory_sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from bog_agents_cli.cmd_memory_sync import (
    _DEFAULT_MEMORY_BRANCH,
    format_memory_help,
    get_memory_branch,
    list_memory_files,
    sync_memory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_git(returncode=0, stdout="", stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# get_memory_branch
# ---------------------------------------------------------------------------

class TestGetMemoryBranch:
    def test_returns_default_when_not_in_git_repo(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync._run_git", return_value=_mock_git(returncode=1)):
            result = get_memory_branch(tmp_path)
        assert result == _DEFAULT_MEMORY_BRANCH

    def test_returns_default_when_config_missing(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = get_memory_branch(tmp_path)
        assert result == _DEFAULT_MEMORY_BRANCH

    def test_reads_branch_from_config(self, tmp_path):
        config_dir = tmp_path / ".bog-agents"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[memory]\nbranch = "custom-branch"\n')
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = get_memory_branch(tmp_path)
        assert result == "custom-branch"

    def test_returns_default_when_config_has_no_memory_section(self, tmp_path):
        config_dir = tmp_path / ".bog-agents"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[other]\nkey = "value"\n')
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = get_memory_branch(tmp_path)
        assert result == _DEFAULT_MEMORY_BRANCH

    def test_returns_default_when_config_corrupt(self, tmp_path):
        config_dir = tmp_path / ".bog-agents"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("{not valid toml !!!")
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = get_memory_branch(tmp_path)
        assert result == _DEFAULT_MEMORY_BRANCH


# ---------------------------------------------------------------------------
# sync_memory
# ---------------------------------------------------------------------------

class TestSyncMemory:
    def test_invalid_direction_returns_error(self, tmp_path):
        result = sync_memory(tmp_path, direction="sideways")
        assert "Unknown direction" in result

    def test_pull_direction_calls_pull(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync.get_memory_branch", return_value="team-memory"):
            with patch("bog_agents_cli.cmd_memory_sync._pull_memory", return_value="pulled") as mock_pull:
                with patch("bog_agents_cli.cmd_memory_sync._push_memory", return_value="pushed") as mock_push:
                    result = sync_memory(tmp_path, direction="pull")
        mock_pull.assert_called_once()
        mock_push.assert_not_called()
        assert "pulled" in result

    def test_push_direction_calls_push(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync.get_memory_branch", return_value="team-memory"):
            with patch("bog_agents_cli.cmd_memory_sync._pull_memory", return_value="pulled") as mock_pull:
                with patch("bog_agents_cli.cmd_memory_sync._push_memory", return_value="pushed") as mock_push:
                    result = sync_memory(tmp_path, direction="push")
        mock_push.assert_called_once()
        mock_pull.assert_not_called()
        assert "pushed" in result

    def test_both_calls_pull_and_push(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync.get_memory_branch", return_value="team-memory"):
            with patch("bog_agents_cli.cmd_memory_sync._pull_memory", return_value="pulled") as mock_pull:
                with patch("bog_agents_cli.cmd_memory_sync._push_memory", return_value="pushed") as mock_push:
                    result = sync_memory(tmp_path, direction="both")
        mock_pull.assert_called_once()
        mock_push.assert_called_once()
        assert "pulled" in result
        assert "pushed" in result

    def test_empty_messages_not_included(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync.get_memory_branch", return_value="team-memory"):
            with patch("bog_agents_cli.cmd_memory_sync._pull_memory", return_value=""):
                with patch("bog_agents_cli.cmd_memory_sync._push_memory", return_value="pushed"):
                    result = sync_memory(tmp_path, direction="both")
        # Empty pull message should be filtered out
        assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# list_memory_files
# ---------------------------------------------------------------------------

class TestListMemoryFiles:
    def test_finds_agents_md_in_git_root(self, tmp_path):
        agents_md = tmp_path / "AGENTS.md"
        agents_md.write_text("# Memory\n")
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = list_memory_files(tmp_path)
        assert agents_md in result

    def test_returns_empty_when_no_files_and_not_in_git(self, tmp_path):
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(returncode=1)):
            result = list_memory_files(tmp_path)
        # May return user-global AGENTS.md, but that path is not in tmp_path
        assert all(p != tmp_path / "AGENTS.md" for p in result)

    def test_finds_nested_agents_md(self, tmp_path):
        subdir = tmp_path / "libs" / "pkg"
        subdir.mkdir(parents=True)
        nested = subdir / "AGENTS.md"
        nested.write_text("# Nested\n")
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = list_memory_files(tmp_path)
        assert nested in result

    def test_returns_sorted_list(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("root\n")
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "AGENTS.md").write_text("sub\n")
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = list_memory_files(tmp_path)
        # Result must be sorted
        paths_in_tmp = [p for p in result if str(p).startswith(str(tmp_path))]
        assert paths_in_tmp == sorted(paths_in_tmp)

    def test_deduplicates_results(self, tmp_path):
        agents_md = tmp_path / "AGENTS.md"
        agents_md.write_text("# Memory\n")
        with patch("bog_agents_cli.cmd_memory_sync._run_git",
                   return_value=_mock_git(stdout=str(tmp_path))):
            result = list_memory_files(tmp_path)
        # Each file should appear at most once
        assert len(result) == len(set(result))


# ---------------------------------------------------------------------------
# format_memory_help
# ---------------------------------------------------------------------------

class TestFormatMemoryHelp:
    def test_returns_string(self):
        result = format_memory_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_pull_and_push(self):
        result = format_memory_help()
        assert "pull" in result
        assert "push" in result

    def test_mentions_default_branch(self):
        result = format_memory_help()
        assert "team-memory" in result
