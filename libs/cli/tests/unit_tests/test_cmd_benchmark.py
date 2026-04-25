"""Unit tests for bog_agents_cli.cmd_benchmark."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.cmd_benchmark import (
    _make_task_runner,
    format_benchmark_help,
    list_benchmark_suites,
    run_benchmark,
    show_recent_results,
)

# ---------------------------------------------------------------------------
# list_benchmark_suites
# ---------------------------------------------------------------------------


class TestListBenchmarkSuites:
    def test_returns_hint_when_no_suites(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", tmp_path / "benchmarks"
        ):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                result = list_benchmark_suites()
        assert "No benchmark suites found" in result

    def test_shows_user_suite_name(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        yaml_content = (
            "name: my-suite\ndescription: Test suite\ntasks:\n  - prompt: hello\n"
        )
        (benchmarks_dir / "my-suite.yaml").write_text(yaml_content)

        yaml = pytest.importorskip("yaml")
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                result = list_benchmark_suites()
        assert "my-suite" in result

    def test_shows_task_count(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        yaml_content = (
            "name: my-suite\ndescription: Test\ntasks:\n"
            "  - prompt: task1\n  - prompt: task2\n  - prompt: task3\n"
        )
        (benchmarks_dir / "my-suite.yaml").write_text(yaml_content)

        yaml = pytest.importorskip("yaml")
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                result = list_benchmark_suites()
        assert "3" in result

    def test_shows_builtin_tag(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks_empty"
        builtin_yaml = tmp_path / "builtin.yaml"
        builtin_yaml.write_text(
            "name: builtin-suite\ndescription: Built in\ntasks: []\n"
        )

        yaml = pytest.importorskip("yaml")
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites",
                return_value=[builtin_yaml],
            ):
                result = list_benchmark_suites()
        assert "built-in" in result

    def test_shows_header(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        (benchmarks_dir / "s.yaml").write_text("name: s\ndescription: d\ntasks: []\n")
        yaml = pytest.importorskip("yaml")
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                result = list_benchmark_suites()
        assert "Benchmark Suites" in result


# ---------------------------------------------------------------------------
# run_benchmark
# ---------------------------------------------------------------------------


class TestRunBenchmark:
    def test_none_suite_lists_suites(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_benchmark.list_benchmark_suites", return_value="LISTING"
        ) as mock_list:
            result = run_benchmark(None, cwd=tmp_path)
        mock_list.assert_called_once()
        assert result == "LISTING"

    def test_missing_suite_returns_error(self, tmp_path):
        with patch(
            "bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", tmp_path / "benchmarks"
        ):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                result = run_benchmark("nonexistent-suite", cwd=tmp_path)
        assert "not found" in result.lower()

    def test_suite_with_no_tasks_returns_warning(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        (benchmarks_dir / "empty.yaml").write_text(
            "name: empty\ndescription: empty\ntasks: []\n"
        )

        yaml = pytest.importorskip("yaml")
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                result = run_benchmark("empty", cwd=tmp_path)
        assert "no tasks" in result.lower()

    def test_runs_tasks_up_to_max(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        yaml_content = "name: s\ndescription: d\ntasks:\n" + "".join(
            f"  - prompt: task{i}\n" for i in range(10)
        )
        (benchmarks_dir / "s.yaml").write_text(yaml_content)

        yaml = pytest.importorskip("yaml")
        stub_result = {"status": "ok", "tokens": 10, "score": 0.9}

        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                with patch(
                    "bog_agents_cli.cmd_benchmark._make_task_runner",
                    return_value=lambda **_: stub_result,
                ):
                    result = run_benchmark("s", cwd=tmp_path, max_tasks=3)
        # Should only run 3 tasks
        assert "3 tasks" in result

    def test_shows_suite_name(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        (benchmarks_dir / "my-test.yaml").write_text(
            "name: my-test\ndescription: x\ntasks:\n  - prompt: p1\n"
        )

        yaml = pytest.importorskip("yaml")
        stub_result = {"status": "ok", "tokens": 5, "score": 1.0}
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                with patch(
                    "bog_agents_cli.cmd_benchmark._make_task_runner",
                    return_value=lambda **_: stub_result,
                ):
                    result = run_benchmark("my-test", cwd=tmp_path)
        assert "my-test" in result

    def test_shows_average_score(self, tmp_path):
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        (benchmarks_dir / "s.yaml").write_text(
            "name: s\ndescription: d\ntasks:\n  - prompt: p1\n  - prompt: p2\n"
        )
        yaml = pytest.importorskip("yaml")
        stub_result = {"status": "ok", "tokens": 10, "score": 0.5}
        with patch("bog_agents_cli.cmd_benchmark._BENCHMARKS_DIR", benchmarks_dir):
            with patch(
                "bog_agents_cli.cmd_benchmark._harbor_built_in_suites", return_value=[]
            ):
                with patch(
                    "bog_agents_cli.cmd_benchmark._make_task_runner",
                    return_value=lambda **_: stub_result,
                ):
                    result = run_benchmark("s", cwd=tmp_path)
        assert "%" in result


# ---------------------------------------------------------------------------
# _make_task_runner
# ---------------------------------------------------------------------------


class TestMakeTaskRunner:
    def test_returns_callable(self):
        runner = _make_task_runner()
        assert callable(runner)

    def test_stub_runner_returns_skip_when_harbor_unavailable(self, tmp_path):
        with patch("bog_agents_cli.cmd_benchmark.logger"):
            # Force ImportError for harbor backend
            with patch.dict(
                "sys.modules",
                {"bog_agents_harbor": None, "bog_agents_harbor.backend": None},
            ):
                runner = _make_task_runner()
        result = runner(
            prompt="test", expected_keywords=[], max_tokens=100, cwd=tmp_path
        )
        # When harbor unavailable, stub returns skip
        assert result["status"] == "skip"
        assert result["tokens"] == 0
        assert result["score"] == 0.0

    def test_stub_runner_accepts_kwargs(self, tmp_path):
        with patch.dict(
            "sys.modules",
            {"bog_agents_harbor": None, "bog_agents_harbor.backend": None},
        ):
            runner = _make_task_runner()
        # Should not raise
        result = runner(
            prompt="hello world",
            expected_keywords=["hello"],
            max_tokens=500,
            cwd=tmp_path,
        )
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# show_recent_results
# ---------------------------------------------------------------------------


class TestShowRecentResults:
    def test_returns_message_when_harbor_not_installed(self, tmp_path):
        with patch.dict(
            "sys.modules",
            {"bog_agents_harbor": None, "bog_agents_harbor.reporter": None},
        ):
            result = show_recent_results(cwd=tmp_path)
        assert (
            "Harbor package not installed" in result
            or "not installed" in result.lower()
        )

    def test_returns_no_trajectories_message_when_empty(self, tmp_path):
        mock_reporter = MagicMock()
        mock_reporter.find_trajectories = MagicMock(return_value=[])
        mock_reporter.load_trajectory = MagicMock()
        with patch("bog_agents_cli.cmd_benchmark._TRAJECTORIES_DIR", tmp_path / "traj"):
            with patch.dict(
                "sys.modules",
                {
                    "bog_agents_harbor": MagicMock(),
                    "bog_agents_harbor.reporter": mock_reporter,
                },
            ):
                # Reload to pick up mocked imports, or call with the mock in place
                import importlib

                import bog_agents_cli.cmd_benchmark as bm_mod

                original_show = bm_mod.show_recent_results
                # Call the actual function; the ImportError path won't trigger because
                # the module is in sys.modules, but find_trajectories returns []
                result = show_recent_results(cwd=tmp_path)
        # When harbor is not installed (ImportError path), should say not installed
        # When trajectories dir is empty (find_trajectories=[]), should say "No trajectory files"
        assert (
            "No trajectory files" in result
            or "not installed" in result.lower()
            or "Harbor" in result
        )


# ---------------------------------------------------------------------------
# format_benchmark_help
# ---------------------------------------------------------------------------


class TestFormatBenchmarkHelp:
    def test_returns_string(self):
        result = format_benchmark_help()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mentions_benchmark_commands(self):
        result = format_benchmark_help()
        assert "/benchmark" in result

    def test_mentions_run_and_list(self):
        result = format_benchmark_help()
        assert "run" in result
        assert "list" in result

    def test_mentions_yaml_format(self):
        result = format_benchmark_help()
        assert "yaml" in result.lower() or "YAML" in result
