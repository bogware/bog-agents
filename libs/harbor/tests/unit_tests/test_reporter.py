"""Unit tests for bog_agents_harbor.reporter."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003

import pytest

from bog_agents_harbor.reporter import (
    TrajectoryReport,
    _parse_trajectory,
    find_trajectories,
    format_summary,
    format_tool_usage,
    load_all_trajectories,
    load_trajectory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trajectory(
    *,
    session_id: str = "test-session-001",
    model: str = "claude-sonnet-4-6",
    steps: list[dict] | None = None,
    prompt_tokens: int | None = 1000,
    completion_tokens: int | None = 200,
    reward: float | None = None,
) -> dict:
    """Build a minimal but valid ATIF v1.2 trajectory dict."""
    if steps is None:
        steps = [
            {
                "step_id": 1,
                "timestamp": "2025-01-01T00:00:00+00:00",
                "source": "user",
                "message": "Write a hello world script",
            },
            {
                "step_id": 2,
                "timestamp": "2025-01-01T00:00:01+00:00",
                "source": "agent",
                "message": "I'll create that for you.",
                "tool_calls": [
                    {
                        "tool_call_id": "tc-001",
                        "function_name": "bash",
                        "arguments": {"command": "echo hello"},
                    }
                ],
                "observation": {
                    "results": [
                        {
                            "source_call_id": "tc-001",
                            "content": "hello\n",
                        }
                    ]
                },
            },
        ]
    data: dict = {
        "schema_version": "ATIF-v1.2",
        "session_id": session_id,
        "agent": {
            "name": "bog-agents-harbor",
            "version": "0.0.1",
            "model_name": model,
        },
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": prompt_tokens,
            "total_completion_tokens": completion_tokens,
            "total_steps": len(steps),
        },
    }
    if reward is not None:
        data["reward"] = reward
    return data


@pytest.fixture
def simple_trajectory() -> dict:
    return _make_trajectory()


@pytest.fixture
def tmp_trajectory_file(tmp_path: Path) -> Path:
    """Write a valid trajectory to a temp file and return the path."""
    data = _make_trajectory(session_id="file-session", reward=0.85)
    p = tmp_path / "trajectory.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# _parse_trajectory
# ---------------------------------------------------------------------------


class TestParseTrajectory:
    def test_basic_fields(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        assert report.session_id == "test-session-001"
        assert report.agent_name == "bog-agents-harbor"
        assert report.agent_version == "0.0.1"
        assert report.model_name == "claude-sonnet-4-6"
        assert report.schema_version == "ATIF-v1.2"

    def test_steps_parsed(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        assert len(report.steps) == 2
        assert report.steps[0].source == "user"
        assert report.steps[1].source == "agent"

    def test_tool_calls_parsed(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        agent_step = report.steps[1]
        assert len(agent_step.tool_calls) == 1
        assert agent_step.tool_calls[0].function_name == "bash"
        assert agent_step.tool_calls[0].tool_call_id == "tc-001"

    def test_observation_count(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        assert report.steps[1].observation_count == 1

    def test_token_metrics(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        assert report.total_prompt_tokens == 1000
        assert report.total_completion_tokens == 200
        assert report.total_tokens == 1200

    def test_reward_absent(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        assert report.reward is None

    def test_reward_present(self) -> None:
        data = _make_trajectory(reward=0.75)
        report = _parse_trajectory(data)
        assert report.reward == pytest.approx(0.75)

    def test_missing_tokens_gives_none(self) -> None:
        data = _make_trajectory(prompt_tokens=None, completion_tokens=None)
        data["final_metrics"].pop("total_prompt_tokens", None)
        data["final_metrics"].pop("total_completion_tokens", None)
        report = _parse_trajectory(data)
        assert report.total_tokens is None

    def test_empty_steps(self) -> None:
        data = _make_trajectory(steps=[])
        report = _parse_trajectory(data)
        assert report.steps == []
        assert report.tool_call_count == 0

    def test_no_agent_key(self) -> None:
        data = _make_trajectory()
        del data["agent"]
        report = _parse_trajectory(data)
        assert report.agent_name == ""
        assert report.model_name == ""

    def test_source_path_stored(self, tmp_path: Path) -> None:
        p = tmp_path / "traj.json"
        data = _make_trajectory()
        report = _parse_trajectory(data, source_path=p)
        assert report.raw_path == p


# ---------------------------------------------------------------------------
# TrajectoryReport computed properties
# ---------------------------------------------------------------------------


class TestTrajectoryReportProperties:
    def test_agent_steps(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        agent_steps = report.agent_steps
        assert len(agent_steps) == 1
        assert all(s.source == "agent" for s in agent_steps)

    def test_tool_call_count(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        assert report.tool_call_count == 1

    def test_total_tokens_none_when_both_none(self) -> None:
        report = TrajectoryReport(
            schema_version="",
            session_id="",
            agent_name="",
            agent_version="",
            model_name="",
            steps=[],
            total_prompt_tokens=None,
            total_completion_tokens=None,
            total_steps=0,
            reward=None,
        )
        assert report.total_tokens is None

    def test_total_tokens_sums_partial(self) -> None:
        report = TrajectoryReport(
            schema_version="",
            session_id="",
            agent_name="",
            agent_version="",
            model_name="",
            steps=[],
            total_prompt_tokens=500,
            total_completion_tokens=None,
            total_steps=0,
            reward=None,
        )
        assert report.total_tokens == 500


# ---------------------------------------------------------------------------
# load_trajectory
# ---------------------------------------------------------------------------


class TestLoadTrajectory:
    def test_load_valid_file(self, tmp_trajectory_file: Path) -> None:
        report = load_trajectory(tmp_trajectory_file)
        assert report.session_id == "file-session"
        assert report.reward == pytest.approx(0.85)
        assert report.raw_path == tmp_trajectory_file

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_trajectory(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{ not valid json }", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_trajectory(p)

    def test_str_path_accepted(self, tmp_trajectory_file: Path) -> None:
        report = load_trajectory(str(tmp_trajectory_file))
        assert report.session_id == "file-session"


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


class TestFormatSummary:
    def test_basic_output(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        summary = format_summary(report)
        assert "test-session-001" in summary
        assert "claude-sonnet-4-6" in summary
        assert "bog-agents-harbor" in summary

    def test_token_count_in_summary(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        summary = format_summary(report)
        assert "1,200" in summary

    def test_reward_bar_shown(self) -> None:
        data = _make_trajectory(reward=0.8)
        report = _parse_trajectory(data)
        summary = format_summary(report)
        assert "80.0%" in summary
        assert "█" in summary

    def test_verbose_includes_steps(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        summary = format_summary(report, verbose=True)
        assert "user" in summary
        assert "agent" in summary
        assert "bash" in summary

    def test_no_reward_no_bar(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        summary = format_summary(report)
        assert "Reward" not in summary


# ---------------------------------------------------------------------------
# format_tool_usage
# ---------------------------------------------------------------------------


class TestFormatToolUsage:
    def test_counts_tools(self, simple_trajectory: dict) -> None:
        report = _parse_trajectory(simple_trajectory)
        output = format_tool_usage(report)
        assert "bash" in output
        assert "1" in output

    def test_no_calls_message(self) -> None:
        data = _make_trajectory(steps=[])
        report = _parse_trajectory(data)
        output = format_tool_usage(report)
        assert "No tool calls" in output

    def test_multiple_calls_ranked(self) -> None:
        steps = [
            {
                "step_id": i,
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {"tool_call_id": f"tc-{i}a", "function_name": "bash", "arguments": {}},
                    {"tool_call_id": f"tc-{i}b", "function_name": "read", "arguments": {}},
                ]
                if i % 2 == 0
                else [{"tool_call_id": f"tc-{i}", "function_name": "bash", "arguments": {}}],
            }
            for i in range(4)
        ]
        data = _make_trajectory(steps=steps)
        report = _parse_trajectory(data)
        output = format_tool_usage(report)
        # bash should appear before read
        bash_pos = output.index("bash")
        read_pos = output.index("read")
        assert bash_pos < read_pos


# ---------------------------------------------------------------------------
# find_trajectories / load_all_trajectories
# ---------------------------------------------------------------------------


class TestFindTrajectories:
    def test_finds_files(self, tmp_path: Path) -> None:
        sub = tmp_path / "run-001"
        sub.mkdir()
        (sub / "trajectory.json").write_text(
            json.dumps(_make_trajectory(session_id="run-001")), encoding="utf-8"
        )
        results = find_trajectories(tmp_path)
        assert len(results) == 1
        assert results[0].name == "trajectory.json"

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        results = find_trajectories(tmp_path / "nonexistent")
        assert results == []

    def test_respects_limit(self, tmp_path: Path) -> None:
        for i in range(5):
            d = tmp_path / f"run-{i:03}"
            d.mkdir()
            (d / "trajectory.json").write_text(
                json.dumps(_make_trajectory(session_id=f"session-{i}")), encoding="utf-8"
            )
        results = find_trajectories(tmp_path, limit=3)
        assert len(results) == 3

    def test_nested_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / "trajectory.json").write_text(json.dumps(_make_trajectory()), encoding="utf-8")
        results = find_trajectories(tmp_path)
        assert len(results) == 1

    def test_load_all_skips_invalid(self, tmp_path: Path) -> None:
        good = tmp_path / "good"
        good.mkdir()
        (good / "trajectory.json").write_text(
            json.dumps(_make_trajectory(session_id="good-one")), encoding="utf-8"
        )
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "trajectory.json").write_text("not json", encoding="utf-8")

        reports = load_all_trajectories(tmp_path)
        assert len(reports) == 1
        assert reports[0].session_id == "good-one"
