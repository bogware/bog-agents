"""Unit tests for bog_agents_cli.cmd_test."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_test import (
    _parse_go_output,
    _parse_jest_output,
    _parse_pytest_output,
    detect_test_framework,
    find_test_file,
    format_test_help,
    run_tests,
)

# ---------------------------------------------------------------------------
# detect_test_framework
# ---------------------------------------------------------------------------


class TestDetectTestFramework:
    def test_detects_pytest_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        result = detect_test_framework(tmp_path)
        assert result == "pytest"

    def test_detects_pytest_via_pytest_keyword(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("pytest = true\n")
        result = detect_test_framework(tmp_path)
        assert result == "pytest"

    def test_detects_vitest_from_package_json(self, tmp_path):
        pkg = {"devDependencies": {"vitest": "^0.34.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = detect_test_framework(tmp_path)
        assert result == "vitest"

    def test_detects_jest_from_package_json(self, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = detect_test_framework(tmp_path)
        assert result == "jest"

    def test_vitest_wins_over_jest(self, tmp_path):
        pkg = {"devDependencies": {"vitest": "^1.0.0", "jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        result = detect_test_framework(tmp_path)
        assert result == "vitest"

    def test_detects_go_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/mymodule\ngo 1.21\n")
        result = detect_test_framework(tmp_path)
        assert result == "go"

    def test_detects_cargo_from_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "myproject"\n')
        result = detect_test_framework(tmp_path)
        assert result == "cargo"

    def test_detects_maven_from_pom_xml(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>")
        result = detect_test_framework(tmp_path)
        assert result == "maven"

    def test_returns_none_for_empty_dir(self, tmp_path):
        with patch("bog_agents_cli.cmd_test.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = detect_test_framework(tmp_path)
        assert result is None

    def test_handles_corrupt_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{invalid json")
        result = detect_test_framework(tmp_path)
        assert result is None

    def test_pytest_fallback_with_tests_dir(self, tmp_path):
        (tmp_path / "tests").mkdir()
        with patch("bog_agents_cli.cmd_test.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = detect_test_framework(tmp_path)
        assert result == "pytest"


# ---------------------------------------------------------------------------
# find_test_file
# ---------------------------------------------------------------------------


class TestFindTestFile:
    def test_finds_test_prefix_convention(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("pass")
        test_file = tmp_path / "test_module.py"
        test_file.write_text("pass")
        result = find_test_file(Path("module.py"), tmp_path)
        assert result == test_file

    def test_finds_suffix_convention(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("pass")
        test_file = tmp_path / "module_test.py"
        test_file.write_text("pass")
        result = find_test_file(Path("module.py"), tmp_path)
        assert result == test_file

    def test_finds_test_in_tests_subdir(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("pass")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_module.py"
        test_file.write_text("pass")
        result = find_test_file(Path("module.py"), tmp_path)
        assert result == test_file

    def test_returns_none_when_not_found(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("pass")
        result = find_test_file(Path("module.py"), tmp_path)
        assert result is None

    def test_finds_js_test_file(self, tmp_path):
        src = tmp_path / "component.ts"
        src.write_text("export default {}")
        test_file = tmp_path / "component.test.ts"
        test_file.write_text("test('x', () => {})")
        result = find_test_file(Path("component.ts"), tmp_path)
        assert result == test_file

    def test_finds_go_test_file(self, tmp_path):
        src = tmp_path / "main.go"
        src.write_text("package main")
        test_file = tmp_path / "main_test.go"
        test_file.write_text("package main")
        result = find_test_file(Path("main.go"), tmp_path)
        assert result == test_file

    def test_absolute_source_path(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("pass")
        test_file = tmp_path / "test_module.py"
        test_file.write_text("pass")
        result = find_test_file(src, tmp_path)
        assert result == test_file


# ---------------------------------------------------------------------------
# _parse_pytest_output
# ---------------------------------------------------------------------------


class TestParsePytestOutput:
    def test_parses_all_passed(self):
        output = "3 passed in 0.45s"
        counts = _parse_pytest_output(output)
        assert counts["passed"] == 3
        assert counts["failed"] == 0

    def test_parses_mixed_results(self):
        output = "5 passed, 2 failed, 1 error in 1.2s"
        counts = _parse_pytest_output(output)
        assert counts["passed"] == 5
        assert counts["failed"] == 2
        assert counts["error"] == 1

    def test_parses_skipped(self):
        output = "4 passed, 1 skipped in 0.3s"
        counts = _parse_pytest_output(output)
        assert counts["passed"] == 4
        assert counts["skipped"] == 1

    def test_empty_output(self):
        counts = _parse_pytest_output("")
        assert counts["passed"] == 0
        assert counts["failed"] == 0


# ---------------------------------------------------------------------------
# _parse_jest_output
# ---------------------------------------------------------------------------


class TestParseJestOutput:
    def test_parses_passed(self):
        output = "Tests: 5 passed, 5 total"
        counts = _parse_jest_output(output)
        assert counts["passed"] == 5

    def test_parses_failed(self):
        output = "Tests: 3 failed, 3 total"
        counts = _parse_jest_output(output)
        assert counts["failed"] == 3

    def test_parses_skipped(self):
        output = "Tests: 2 skipped, 4 passed"
        counts = _parse_jest_output(output)
        assert counts["skipped"] == 2
        assert counts["passed"] == 4

    def test_empty_output(self):
        counts = _parse_jest_output("")
        assert counts["passed"] == 0


# ---------------------------------------------------------------------------
# _parse_go_output
# ---------------------------------------------------------------------------


class TestParseGoOutput:
    def test_detects_pass_line(self):
        output = "ok  \tgithub.com/org/proj\t0.123s"
        counts = _parse_go_output(output)
        assert counts["passed"] >= 1

    def test_detects_fail_line(self):
        output = "FAIL\tgithub.com/org/proj [build failed]"
        counts = _parse_go_output(output)
        assert counts["failed"] >= 1

    def test_empty_output(self):
        counts = _parse_go_output("")
        assert counts["passed"] == 0
        assert counts["failed"] == 0


# ---------------------------------------------------------------------------
# run_tests
# ---------------------------------------------------------------------------


class TestRunTests:
    def _mock_completed(self, returncode=0, stdout="3 passed in 0.45s", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_no_framework_returns_message(self, tmp_path):
        with patch("bog_agents_cli.cmd_test.detect_test_framework", return_value=None):
            result = run_tests(cwd=tmp_path)
        assert "No test framework detected" in result

    def test_runs_pytest_command(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="pytest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run",
                return_value=self._mock_completed(),
            ) as mock_run:
                run_tests(cwd=tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "pytest" in cmd

    def test_runs_jest_command(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="jest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run",
                return_value=self._mock_completed(),
            ) as mock_run:
                run_tests(cwd=tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "jest" in cmd

    def test_shows_passed_count(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="pytest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run",
                return_value=self._mock_completed(stdout="5 passed in 1s"),
            ):
                result = run_tests(cwd=tmp_path)
        assert "5 passed" in result

    def test_handles_timeout(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="pytest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run",
                side_effect=subprocess.TimeoutExpired("pytest", 60),
            ):
                result = run_tests(cwd=tmp_path, timeout=60)
        assert "timed out" in result.lower()

    def test_handles_command_not_found(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="pytest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run", side_effect=FileNotFoundError
            ):
                result = run_tests(cwd=tmp_path)
        assert "not found" in result.lower()

    def test_unknown_framework_returns_error(self, tmp_path):
        result = run_tests(cwd=tmp_path, framework="unknown-fw")
        assert "Unknown framework" in result

    def test_passes_test_file_arg(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="pytest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run",
                return_value=self._mock_completed(),
            ) as mock_run:
                run_tests(cwd=tmp_path, test_file="tests/test_foo.py")
        cmd = mock_run.call_args[0][0]
        assert "tests/test_foo.py" in cmd

    def test_shows_failed_output_when_failures(self, tmp_path):
        output = "1 failed\nFAILED test_foo.py::test_bar - AssertionError"
        with patch(
            "bog_agents_cli.cmd_test.detect_test_framework", return_value="pytest"
        ):
            with patch(
                "bog_agents_cli.cmd_test.subprocess.run",
                return_value=self._mock_completed(returncode=1, stdout=output),
            ):
                result = run_tests(cwd=tmp_path)
        assert "FAILED" in result or "failed" in result


# ---------------------------------------------------------------------------
# format_test_help
# ---------------------------------------------------------------------------


class TestFormatTestHelp:
    def test_returns_string(self):
        result = format_test_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_supported_frameworks(self):
        result = format_test_help()
        assert "pytest" in result
        assert "jest" in result

    def test_mentions_test_command(self):
        result = format_test_help()
        assert "/test" in result
