"""Unit tests for bog_agents_cli.cmd_checkpoint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bog_agents_cli.cmd_checkpoint import (
    delete_checkpoint,
    format_checkpoint_help,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_checkpoint_path(tmp_path: Path):
    """Context manager: redirect _CHECKPOINTS_PATH to a temp file."""
    checkpoint_file = tmp_path / "checkpoints.json"
    return patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file)


# ---------------------------------------------------------------------------
# list_checkpoints
# ---------------------------------------------------------------------------


class TestListCheckpoints:
    def test_empty_when_no_file(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = list_checkpoints()
        assert "No checkpoints saved yet" in result

    def test_shows_checkpoint_names(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text(
            json.dumps(
                {
                    "my-checkpoint": {
                        "thread_id": "abc123",
                        "created_at": "2024-01-01 12:00 UTC",
                        "description": "test desc",
                    }
                }
            )
        )
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = list_checkpoints()
        assert "my-checkpoint" in result
        assert "abc123" in result

    def test_shows_multiple_checkpoints(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text(
            json.dumps(
                {
                    "cp-1": {
                        "thread_id": "t1",
                        "created_at": "2024-01-01",
                        "description": "",
                    },
                    "cp-2": {
                        "thread_id": "t2",
                        "created_at": "2024-01-02",
                        "description": "",
                    },
                }
            )
        )
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = list_checkpoints()
        assert "cp-1" in result
        assert "cp-2" in result

    def test_truncates_long_thread_id(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        long_tid = "a" * 50
        checkpoint_file.write_text(
            json.dumps(
                {
                    "cp": {
                        "thread_id": long_tid,
                        "created_at": "2024-01-01",
                        "description": "",
                    },
                }
            )
        )
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = list_checkpoints()
        # Thread ID is truncated with ellipsis after 12 chars
        assert "\u2026" in result

    def test_handles_corrupt_json(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text("not valid json{{")
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = list_checkpoints()
        assert "No checkpoints saved yet" in result


# ---------------------------------------------------------------------------
# save_checkpoint
# ---------------------------------------------------------------------------


class TestSaveCheckpoint:
    def test_saves_new_checkpoint(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = save_checkpoint("thread-123", "my-cp")
        assert "saved" in result.lower()
        assert "my-cp" in result

    def test_overwrite_existing(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            save_checkpoint("old-thread", "my-cp")
            result = save_checkpoint("new-thread", "my-cp")
        assert "updated" in result.lower() or "overwrote" in result.lower()

    def test_invalid_name_returns_error(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = save_checkpoint("thread-123", "invalid name!")
        assert "Invalid checkpoint name" in result

    def test_name_too_long_returns_error(self, tmp_path):
        long_name = "a" * 65
        with _patch_checkpoint_path(tmp_path):
            result = save_checkpoint("thread-123", long_name)
        assert "Invalid checkpoint name" in result

    def test_stores_description(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            save_checkpoint("t", "my-cp", description="my description")
        data = json.loads(checkpoint_file.read_text())
        assert data["my-cp"]["description"] == "my description"

    def test_stores_thread_id(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            save_checkpoint("thread-xyz", "cp")
        data = json.loads(checkpoint_file.read_text())
        assert data["cp"]["thread_id"] == "thread-xyz"

    def test_valid_name_with_digits_and_hyphens(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = save_checkpoint("t", "cp-1_2")
        assert "saved" in result.lower()


# ---------------------------------------------------------------------------
# load_checkpoint
# ---------------------------------------------------------------------------


class TestLoadCheckpoint:
    def test_returns_thread_id_for_existing(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text(
            json.dumps(
                {
                    "my-cp": {
                        "thread_id": "thread-abc",
                        "created_at": "2024-01-01",
                        "description": "",
                    },
                }
            )
        )
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = load_checkpoint("my-cp")
        assert result == "thread-abc"

    def test_returns_none_for_missing(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = load_checkpoint("nonexistent")
        assert result is None

    def test_returns_none_when_no_file(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = load_checkpoint("any-name")
        assert result is None

    def test_returns_none_on_corrupt_file(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text("{corrupt")
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = load_checkpoint("cp")
        assert result is None


# ---------------------------------------------------------------------------
# delete_checkpoint
# ---------------------------------------------------------------------------


class TestDeleteCheckpoint:
    def test_deletes_existing_checkpoint(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text(
            json.dumps(
                {
                    "my-cp": {
                        "thread_id": "t",
                        "created_at": "2024-01-01",
                        "description": "",
                    },
                }
            )
        )
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            result = delete_checkpoint("my-cp")
        assert "deleted" in result.lower()
        data = json.loads(checkpoint_file.read_text())
        assert "my-cp" not in data

    def test_returns_error_for_missing(self, tmp_path):
        with _patch_checkpoint_path(tmp_path):
            result = delete_checkpoint("nonexistent")
        assert "not found" in result.lower()

    def test_other_checkpoints_preserved(self, tmp_path):
        checkpoint_file = tmp_path / "checkpoints.json"
        checkpoint_file.write_text(
            json.dumps(
                {
                    "cp-1": {
                        "thread_id": "t1",
                        "created_at": "2024-01-01",
                        "description": "",
                    },
                    "cp-2": {
                        "thread_id": "t2",
                        "created_at": "2024-01-01",
                        "description": "",
                    },
                }
            )
        )
        with patch("bog_agents_cli.cmd_checkpoint._CHECKPOINTS_PATH", checkpoint_file):
            delete_checkpoint("cp-1")
        data = json.loads(checkpoint_file.read_text())
        assert "cp-1" not in data
        assert "cp-2" in data


# ---------------------------------------------------------------------------
# format_checkpoint_help
# ---------------------------------------------------------------------------


class TestFormatCheckpointHelp:
    def test_returns_string(self):
        result = format_checkpoint_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_save_and_load(self):
        result = format_checkpoint_help()
        assert "save" in result
        assert "load" in result

    def test_mentions_delete(self):
        result = format_checkpoint_help()
        assert "delete" in result
