"""Unit tests for bog_agents_cli.cmd_explain."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_explain import (
    _grep_tool,
    _looks_like_file,
    _read_lines,
    build_explain_prompt,
    format_explain_help,
    format_explain_not_found,
    gather_explain_context,
)

# ---------------------------------------------------------------------------
# _grep_tool
# ---------------------------------------------------------------------------


class TestGrepTool:
    def test_returns_rg_when_available(self):
        with patch("bog_agents_cli.cmd_explain.shutil.which", return_value="/usr/bin/rg"):
            assert _grep_tool() == "rg"

    def test_returns_grep_when_rg_unavailable(self):
        with patch("bog_agents_cli.cmd_explain.shutil.which", return_value=None):
            assert _grep_tool() == "grep"


# ---------------------------------------------------------------------------
# _looks_like_file
# ---------------------------------------------------------------------------

class TestLooksLikeFile:
    def test_path_with_slash_is_file(self):
        assert _looks_like_file("src/agent.py") is True

    def test_path_with_backslash_is_file(self):
        assert _looks_like_file("src\\agent.py") is True

    def test_py_extension_is_file(self):
        assert _looks_like_file("module.py") is True

    def test_ts_extension_is_file(self):
        assert _looks_like_file("component.ts") is True

    def test_plain_symbol_is_not_file(self):
        assert _looks_like_file("create_agent") is False

    def test_symbol_with_no_ext_is_not_file(self):
        assert _looks_like_file("MyClass") is False

    def test_go_extension_is_file(self):
        assert _looks_like_file("main.go") is True

    def test_rs_extension_is_file(self):
        assert _looks_like_file("lib.rs") is True


# ---------------------------------------------------------------------------
# _read_lines
# ---------------------------------------------------------------------------

class TestReadLines:
    def test_reads_slice(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = _read_lines(f, 2, 4)
        assert result == "line2\nline3\nline4"

    def test_clamps_start_at_one(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("a\nb\nc\n")
        result = _read_lines(f, 0, 2)
        assert result == "a\nb"

    def test_clamps_end_at_file_length(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("a\nb\nc\n")
        result = _read_lines(f, 1, 100)
        assert "a" in result
        assert "c" in result

    def test_returns_empty_on_oserror(self, tmp_path):
        missing = tmp_path / "nonexistent.txt"
        result = _read_lines(missing, 1, 10)
        assert result == ""

    def test_single_line(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("only line\n")
        result = _read_lines(f, 1, 1)
        assert result == "only line"


# ---------------------------------------------------------------------------
# gather_explain_context - file mode
# ---------------------------------------------------------------------------

class TestGatherExplainContextFile:
    def test_file_target_reads_content(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("import os\ndef foo(): pass\n")
        result = gather_explain_context("module.py", tmp_path)
        assert result["type"] == "file"
        assert "def foo" in result["content"]

    def test_file_target_extracts_imports(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("import os\nimport sys\ndef foo(): pass\n")
        result = gather_explain_context("module.py", tmp_path)
        assert "import os" in result["imports"]

    def test_file_target_missing_returns_unknown(self, tmp_path):
        result = gather_explain_context("nonexistent.py", tmp_path)
        assert result["type"] == "unknown"

    def test_absolute_file_path(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("def bar(): pass\n")
        result = gather_explain_context(str(py_file), tmp_path)
        assert result["type"] == "file"
        assert "def bar" in result["content"]

    def test_file_location_set(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("x = 1\n")
        result = gather_explain_context("module.py", tmp_path)
        assert result["location"] != ""


# ---------------------------------------------------------------------------
# gather_explain_context - symbol mode
# ---------------------------------------------------------------------------

class TestGatherExplainContextSymbol:
    def test_unknown_when_not_found(self, tmp_path):
        with patch("bog_agents_cli.cmd_explain._find_definition", return_value=None):
            result = gather_explain_context("nonexistent_symbol", tmp_path)
        assert result["type"] == "unknown"
        assert result["content"] == ""

    def test_symbol_type_when_found(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("\n" * 25 + "def my_func():\n    pass\n")
        with patch("bog_agents_cli.cmd_explain._find_definition", return_value=(py_file, 26)):
            with patch("bog_agents_cli.cmd_explain._find_callers", return_value=[]):
                result = gather_explain_context("my_func", tmp_path)
        assert result["type"] == "symbol"

    def test_location_includes_file_and_line(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("def my_func():\n    pass\n")
        with patch("bog_agents_cli.cmd_explain._find_definition", return_value=(py_file, 1)):
            with patch("bog_agents_cli.cmd_explain._find_callers", return_value=[]):
                result = gather_explain_context("my_func", tmp_path)
        assert str(py_file) in result["location"]
        assert ":1" in result["location"]

    def test_callers_included_in_result(self, tmp_path):
        py_file = tmp_path / "module.py"
        py_file.write_text("def my_func():\n    pass\n")
        callers = ["other.py:10: my_func()"]
        with patch("bog_agents_cli.cmd_explain._find_definition", return_value=(py_file, 1)):
            with patch("bog_agents_cli.cmd_explain._find_callers", return_value=callers):
                result = gather_explain_context("my_func", tmp_path)
        assert "other.py:10" in result["callers"]


# ---------------------------------------------------------------------------
# build_explain_prompt
# ---------------------------------------------------------------------------

class TestBuildExplainPrompt:
    def test_includes_target_name(self):
        ctx = {"type": "symbol", "content": "", "location": "", "imports": "", "callers": ""}
        result = build_explain_prompt("my_function", ctx)
        assert "my_function" in result

    def test_includes_content_when_present(self):
        ctx = {"type": "symbol", "content": "def my_func(): pass", "location": "foo.py:1", "imports": "", "callers": ""}
        result = build_explain_prompt("my_func", ctx)
        assert "def my_func(): pass" in result

    def test_includes_location_when_present(self):
        ctx = {"type": "symbol", "content": "", "location": "foo.py:42", "imports": "", "callers": ""}
        result = build_explain_prompt("sym", ctx)
        assert "foo.py:42" in result

    def test_includes_imports_when_present(self):
        ctx = {"type": "file", "content": "", "location": "", "imports": "import os\nimport sys", "callers": ""}
        result = build_explain_prompt("module.py", ctx)
        assert "import os" in result

    def test_includes_callers_when_present(self):
        ctx = {"type": "symbol", "content": "", "location": "", "imports": "", "callers": "other.py:5: foo()"}
        result = build_explain_prompt("foo", ctx)
        assert "other.py:5" in result

    def test_omits_empty_sections(self):
        ctx = {"type": "symbol", "content": "", "location": "", "imports": "", "callers": ""}
        result = build_explain_prompt("sym", ctx)
        assert "Relevant source code:" not in result
        assert "Imports" not in result
        assert "Call sites" not in result

    def test_has_numbered_instructions(self):
        ctx = {"type": "symbol", "content": "", "location": "", "imports": "", "callers": ""}
        result = build_explain_prompt("sym", ctx)
        assert "1." in result
        assert "5." in result


# ---------------------------------------------------------------------------
# format_explain_not_found
# ---------------------------------------------------------------------------

class TestFormatExplainNotFound:
    def test_includes_target(self):
        result = format_explain_not_found("my_symbol")
        assert "my_symbol" in result

    def test_includes_suggestions(self):
        result = format_explain_not_found("missing")
        assert "Suggestion" in result

    def test_returns_string(self):
        assert isinstance(format_explain_not_found("x"), str)


# ---------------------------------------------------------------------------
# format_explain_help
# ---------------------------------------------------------------------------

class TestFormatExplainHelp:
    def test_returns_string(self):
        result = format_explain_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_explain_command(self):
        result = format_explain_help()
        assert "/explain" in result

    def test_includes_examples(self):
        result = format_explain_help()
        assert "Example" in result
