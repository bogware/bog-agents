"""Unit tests for bog_agents_cli.cmd_undo."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli import cmd_undo
from bog_agents_cli.cmd_undo import (
    get_last_edit_summary,
    record_edit,
    undo_last_edit,
    undo_via_git,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_state():
    """Reset module-level _last_edit state."""
    cmd_undo._last_edit = None


# ---------------------------------------------------------------------------
# record_edit
# ---------------------------------------------------------------------------

class TestRecordEdit:
    def setup_method(self):
        _reset_state()

    def test_records_existing_file(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("original content")
        record_edit(str(f))
        assert cmd_undo._last_edit is not None
        assert cmd_undo._last_edit["original"] == "original content"

    def test_records_none_for_new_file(self, tmp_path):
        record_edit(str(tmp_path / "nonexistent.py"))
        assert cmd_undo._last_edit["original"] is None

    def test_stores_file_path(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("content")
        record_edit(str(f))
        assert cmd_undo._last_edit["path"] == str(f)

    def test_overwrites_previous_record(self, tmp_path):
        f1 = tmp_path / "first.py"
        f1.write_text("first")
        f2 = tmp_path / "second.py"
        f2.write_text("second")
        record_edit(str(f1))
        record_edit(str(f2))
        assert cmd_undo._last_edit["path"] == str(f2)
        assert cmd_undo._last_edit["original"] == "second"


# ---------------------------------------------------------------------------
# undo_last_edit
# ---------------------------------------------------------------------------

class TestUndoLastEdit:
    def setup_method(self):
        _reset_state()

    def test_nothing_to_undo_message(self):
        result = undo_last_edit()
        assert "Nothing to undo" in result

    def test_restores_previous_content(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("original content")
        record_edit(str(f))
        f.write_text("modified content")
        result = undo_last_edit()
        assert "Undo" in result
        assert f.read_text() == "original content"

    def test_deletes_new_file(self, tmp_path):
        f = tmp_path / "new_file.py"
        # Record before file exists (so original is None)
        record_edit(str(f))
        # Create the file (simulating the agent writing a new file)
        f.write_text("new file content")
        result = undo_last_edit()
        assert "deleted" in result
        assert not f.exists()

    def test_clears_last_edit_after_undo(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("content")
        record_edit(str(f))
        undo_last_edit()
        assert cmd_undo._last_edit is None

    def test_undo_clears_state_so_second_undo_returns_nothing(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("content")
        record_edit(str(f))
        undo_last_edit()
        result = undo_last_edit()
        assert "Nothing to undo" in result

    def test_returns_rich_success_message(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("original")
        record_edit(str(f))
        f.write_text("modified")
        result = undo_last_edit()
        # Should mention the file path
        assert str(f) in result or f.name in result


# ---------------------------------------------------------------------------
# get_last_edit_summary
# ---------------------------------------------------------------------------

class TestGetLastEditSummary:
    def setup_method(self):
        _reset_state()

    def test_returns_none_when_no_edit(self):
        assert get_last_edit_summary() is None

    def test_returns_restore_summary_for_existing_file(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("content")
        record_edit(str(f))
        result = get_last_edit_summary()
        assert result is not None
        assert "Restore" in result or "restore" in result

    def test_returns_delete_summary_for_new_file(self, tmp_path):
        record_edit(str(tmp_path / "new_file.py"))
        result = get_last_edit_summary()
        assert result is not None
        assert "Delete" in result or "delete" in result

    def test_includes_file_path_in_summary(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("content")
        record_edit(str(f))
        result = get_last_edit_summary()
        assert str(f) in result


# ---------------------------------------------------------------------------
# undo_via_git
# ---------------------------------------------------------------------------

class TestUndoViaGit:
    def test_success_returns_reverted_message(self, tmp_path):
        mock_result = MagicMock(returncode=0, stderr="")
        with patch("bog_agents_cli.cmd_undo.subprocess.run", return_value=mock_result):
            result = undo_via_git("src/file.py")
        assert "reverted" in result.lower()

    def test_failure_returns_error_message(self, tmp_path):
        mock_result = MagicMock(returncode=1, stderr="pathspec not in index")
        with patch("bog_agents_cli.cmd_undo.subprocess.run", return_value=mock_result):
            result = undo_via_git("src/file.py")
        assert "failed" in result.lower()
        assert "pathspec not in index" in result

    def test_git_not_found_returns_error(self):
        with patch("bog_agents_cli.cmd_undo.subprocess.run", side_effect=FileNotFoundError):
            result = undo_via_git("src/file.py")
        assert "not found" in result.lower()

    def test_includes_file_path_in_success(self):
        mock_result = MagicMock(returncode=0, stderr="")
        with patch("bog_agents_cli.cmd_undo.subprocess.run", return_value=mock_result):
            result = undo_via_git("src/file.py")
        assert "src/file.py" in result

    def test_includes_file_path_in_failure(self):
        mock_result = MagicMock(returncode=1, stderr="error msg")
        with patch("bog_agents_cli.cmd_undo.subprocess.run", return_value=mock_result):
            result = undo_via_git("src/file.py")
        assert "src/file.py" in result

    def test_passes_file_to_git_checkout(self):
        mock_result = MagicMock(returncode=0, stderr="")
        with patch("bog_agents_cli.cmd_undo.subprocess.run", return_value=mock_result) as mock_run:
            undo_via_git("specific/file.py")
        cmd = mock_run.call_args[0][0]
        assert "specific/file.py" in cmd
        assert "checkout" in cmd
