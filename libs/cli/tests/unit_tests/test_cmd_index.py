"""Unit tests for bog_agents_cli.cmd_index."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_index import (
    _is_binary_or_generated,
    _project_hash,
    _tfidf_score,
    build_index,
    format_index_help,
    index_status,
    search_index,
)

# ---------------------------------------------------------------------------
# _project_hash
# ---------------------------------------------------------------------------


class TestProjectHash:
    def test_returns_8_chars(self, tmp_path):
        h = _project_hash(tmp_path)
        assert len(h) == 8

    def test_same_path_same_hash(self, tmp_path):
        assert _project_hash(tmp_path) == _project_hash(tmp_path)

    def test_different_paths_different_hash(self, tmp_path):
        other = tmp_path / "sub"
        assert _project_hash(tmp_path) != _project_hash(other)


# ---------------------------------------------------------------------------
# _is_binary_or_generated
# ---------------------------------------------------------------------------


class TestIsBinaryOrGenerated:
    def test_pyc_is_binary(self):
        assert _is_binary_or_generated("module.pyc") is True

    def test_png_is_binary(self):
        assert _is_binary_or_generated("image.png") is True

    def test_lock_is_skipped(self):
        assert _is_binary_or_generated("uv.lock") is True

    def test_py_is_not_binary(self):
        assert _is_binary_or_generated("module.py") is False

    def test_ts_is_not_binary(self):
        assert _is_binary_or_generated("component.ts") is False

    def test_min_js_is_binary(self):
        assert _is_binary_or_generated("bundle.min.js") is True

    def test_so_is_binary(self):
        assert _is_binary_or_generated("lib.so") is True

    def test_txt_not_binary(self):
        assert _is_binary_or_generated("README.txt") is False


# ---------------------------------------------------------------------------
# _tfidf_score
# ---------------------------------------------------------------------------


class TestTfIdfScore:
    def test_exact_symbol_match_high_score(self):
        entry = {"symbols": ["MyClass"], "summary": "", "size": 100}
        score = _tfidf_score("myclass", entry, "path/to/file.py")
        assert score >= 100

    def test_prefix_symbol_match(self):
        entry = {"symbols": ["MyClassBase"], "summary": "", "size": 100}
        score = _tfidf_score("myclass", entry, "path/to/file.py")
        assert score > 0

    def test_no_match_returns_zero(self):
        entry = {"symbols": ["OtherClass"], "summary": "unrelated content", "size": 100}
        score = _tfidf_score("xyz_not_present", entry, "path/to/other.py")
        assert score == 0

    def test_filename_match(self):
        entry = {"symbols": [], "summary": "", "size": 100}
        score = _tfidf_score("mymodule", entry, "src/mymodule.py")
        assert score > 0

    def test_summary_match(self):
        entry = {"symbols": [], "summary": "contains keyword here", "size": 100}
        score = _tfidf_score("keyword", entry, "unrelated/path.py")
        assert score > 0

    def test_symbol_exact_beats_summary(self):
        entry = {"symbols": ["keyword"], "summary": "keyword present", "size": 100}
        score_sym = _tfidf_score("keyword", entry, "path.py")
        entry2 = {"symbols": [], "summary": "keyword present", "size": 100}
        score_sum = _tfidf_score("keyword", entry2, "path.py")
        assert score_sym > score_sum


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def _make_fake_index(self, tmp_path: Path) -> Path:
        """Write a minimal index file and return its path."""
        from bog_agents_cli.cmd_index import _index_path

        idx = _index_path(tmp_path)
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(
            json.dumps(
                {
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "root": str(tmp_path),
                    "files": {"a.py": {"symbols": ["foo"], "summary": "x", "size": 10}},
                }
            )
        )
        return idx

    def test_returns_message_if_index_exists(self, tmp_path):
        self._make_fake_index(tmp_path)
        result = build_index(tmp_path)
        assert "already exists" in result

    def test_force_rebuilds_even_if_exists(self, tmp_path):
        self._make_fake_index(tmp_path)
        with patch("bog_agents_cli.cmd_index._list_tracked_files", return_value=[]):
            result = build_index(tmp_path, force=True)
        assert "indexed" in result.lower() or "built" in result.lower()

    def test_build_with_no_files(self, tmp_path):
        with patch("bog_agents_cli.cmd_index._list_tracked_files", return_value=[]):
            result = build_index(tmp_path)
        assert "0 files indexed" in result

    def test_build_indexes_py_file(self, tmp_path):
        py_file = tmp_path / "hello.py"
        py_file.write_text("def hello():\n    pass\n")
        with patch(
            "bog_agents_cli.cmd_index._list_tracked_files", return_value=["hello.py"]
        ):
            with patch(
                "bog_agents_cli.cmd_index._extract_symbols", return_value=["hello"]
            ):
                result = build_index(tmp_path)
        assert "1 files indexed" in result

    def test_skips_binary_files(self, tmp_path):
        binary = tmp_path / "lib.pyc"
        binary.write_bytes(b"\x00\x01\x02")
        with patch(
            "bog_agents_cli.cmd_index._list_tracked_files", return_value=["lib.pyc"]
        ):
            result = build_index(tmp_path)
        assert "0 files indexed" in result

    def test_writes_index_file(self, tmp_path):
        from bog_agents_cli.cmd_index import _index_path

        with patch("bog_agents_cli.cmd_index._list_tracked_files", return_value=[]):
            build_index(tmp_path)
        assert _index_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# search_index
# ---------------------------------------------------------------------------


class TestSearchIndex:
    def _write_index(self, tmp_path: Path, files: dict) -> None:
        from bog_agents_cli.cmd_index import _index_path

        idx = _index_path(tmp_path)
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(UTC).isoformat(),
                    "root": str(tmp_path),
                    "files": files,
                }
            )
        )

    def test_empty_query_returns_hint(self, tmp_path):
        result = search_index("", tmp_path)
        assert "provide a search query" in result.lower()

    def test_no_index_returns_hint(self, tmp_path):
        result = search_index("foo", tmp_path)
        assert "No index found" in result

    def test_finds_matching_symbol(self, tmp_path):
        self._write_index(
            tmp_path,
            {
                "src/agent.py": {
                    "symbols": ["create_agent"],
                    "summary": "agent module",
                    "size": 100,
                },
            },
        )
        result = search_index("create_agent", tmp_path)
        assert "src/agent.py" in result

    def test_no_results_message(self, tmp_path):
        self._write_index(
            tmp_path,
            {
                "src/agent.py": {
                    "symbols": ["OtherClass"],
                    "summary": "unrelated",
                    "size": 100,
                },
            },
        )
        result = search_index("xyz_nonexistent", tmp_path)
        assert "No results" in result

    def test_limit_applied(self, tmp_path):
        files = {
            f"file{i}.py": {"symbols": ["myclass"], "summary": "test", "size": 100}
            for i in range(20)
        }
        self._write_index(tmp_path, files)
        result = search_index("myclass", tmp_path, limit=3)
        # At most 3 result lines (+ header/separator)
        result_lines = [line for line in result.splitlines() if "file" in line]
        assert len(result_lines) <= 3

    def test_returns_formatted_table(self, tmp_path):
        self._write_index(
            tmp_path,
            {
                "src/foo.py": {"symbols": ["bar"], "summary": "bar module", "size": 50},
            },
        )
        result = search_index("bar", tmp_path)
        assert "File" in result
        assert "Score" in result


# ---------------------------------------------------------------------------
# index_status
# ---------------------------------------------------------------------------


class TestIndexStatus:
    def test_no_index_returns_hint(self, tmp_path):
        result = index_status(tmp_path)
        assert "No index found" in result

    def test_shows_file_count(self, tmp_path):
        from bog_agents_cli.cmd_index import _index_path

        idx = _index_path(tmp_path)
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(
            json.dumps(
                {
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "root": str(tmp_path),
                    "files": {
                        "a.py": {"symbols": [], "summary": "", "size": 10},
                        "b.py": {"symbols": [], "summary": "", "size": 20},
                    },
                }
            )
        )
        result = index_status(tmp_path)
        assert "2" in result

    def test_shows_location(self, tmp_path):
        from bog_agents_cli.cmd_index import _index_path

        idx = _index_path(tmp_path)
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text(
            json.dumps({"created_at": "2024-01-01", "root": str(tmp_path), "files": {}})
        )
        result = index_status(tmp_path)
        assert "Location" in result


# ---------------------------------------------------------------------------
# format_index_help
# ---------------------------------------------------------------------------


class TestFormatIndexHelp:
    def test_returns_string(self):
        result = format_index_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_build_and_search(self):
        result = format_index_help()
        assert "build" in result
        assert "search" in result
