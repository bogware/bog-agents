"""Trajectory reporter for Harbor evaluation runs.

Reads ``trajectory.json`` (ATIF v1.2 format) files produced by
:class:`~bog_agents_harbor.bog_agents_wrapper.BogAgentsWrapper` and
generates human-readable summaries for the CLI and logs.

Usage::

    from bog_agents_harbor.reporter import load_trajectory, format_summary

    traj = load_trajectory("/path/to/trajectory.json")
    print(format_summary(traj))
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model (mirrors ATIF v1.2 schema — light read-only view)
# ---------------------------------------------------------------------------


@dataclass
class ToolCallSummary:
    """Condensed view of a single tool call."""

    function_name: str
    tool_call_id: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepSummary:
    """Condensed view of a single trajectory step."""

    step_id: int
    source: str  # "user" | "agent"
    message: str
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    observation_count: int = 0


@dataclass
class TrajectoryReport:
    """Parsed trajectory ready for human display."""

    schema_version: str
    session_id: str
    agent_name: str
    agent_version: str
    model_name: str
    steps: list[StepSummary]
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_steps: int
    reward: float | None  # Harbor reward score if present (0.0-1.0)
    raw_path: Path | None = None

    @property
    def total_tokens(self) -> int | None:
        """Sum of prompt + completion tokens, or None."""
        if self.total_prompt_tokens is None and self.total_completion_tokens is None:
            return None
        return (self.total_prompt_tokens or 0) + (self.total_completion_tokens or 0)

    @property
    def tool_call_count(self) -> int:
        """Total number of tool calls across all steps."""
        return sum(len(s.tool_calls) for s in self.steps)

    @property
    def agent_steps(self) -> list[StepSummary]:
        """Steps where source == 'agent'."""
        return [s for s in self.steps if s.source == "agent"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def load_trajectory(path: str | Path) -> TrajectoryReport:
    """Load and parse a trajectory.json file into a :class:`TrajectoryReport`.

    Args:
        path: Path to the ``trajectory.json`` file.

    Returns:
        Parsed :class:`TrajectoryReport`.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the JSON cannot be parsed or is missing required fields.
    """
    p = Path(path)
    if not p.is_file():
        msg = f"Trajectory file not found: {p}"
        raise FileNotFoundError(msg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON in trajectory file {p}: {exc}"
        raise ValueError(msg) from exc

    return _parse_trajectory(data, source_path=p)


def _parse_trajectory(data: dict[str, Any], *, source_path: Path | None = None) -> TrajectoryReport:
    """Parse raw ATIF dict into a :class:`TrajectoryReport`.

    Args:
        data: Raw parsed JSON dict.
        source_path: Original file path (for display).

    Returns:
        Parsed report.
    """
    agent_data = data.get("agent", {})
    metrics = data.get("final_metrics", {})

    steps: list[StepSummary] = []
    for raw_step in data.get("steps", []):
        tc_summaries = [
            ToolCallSummary(
                function_name=tc.get("function_name", ""),
                tool_call_id=tc.get("tool_call_id", ""),
                arguments=tc.get("arguments", {}),
            )
            for tc in (raw_step.get("tool_calls") or [])
        ]
        obs = raw_step.get("observation") or {}
        obs_count = len(obs.get("results", [])) if isinstance(obs, dict) else 0
        steps.append(
            StepSummary(
                step_id=raw_step.get("step_id", 0),
                source=raw_step.get("source", ""),
                message=raw_step.get("message", ""),
                tool_calls=tc_summaries,
                observation_count=obs_count,
            )
        )

    return TrajectoryReport(
        schema_version=data.get("schema_version", ""),
        session_id=data.get("session_id", ""),
        agent_name=agent_data.get("name", ""),
        agent_version=agent_data.get("version", ""),
        model_name=agent_data.get("model_name", ""),
        steps=steps,
        total_prompt_tokens=metrics.get("total_prompt_tokens"),
        total_completion_tokens=metrics.get("total_completion_tokens"),
        total_steps=metrics.get("total_steps", len(steps)),
        reward=data.get("reward"),
        raw_path=source_path,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_summary(report: TrajectoryReport, *, verbose: bool = False) -> str:
    """Render a human-readable summary of *report*.

    Args:
        report: Parsed trajectory.
        verbose: When True, include per-step details.

    Returns:
        Multi-line string suitable for console output.
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"Harbor Trajectory: {report.session_id}")
    lines.append("=" * 60)
    lines.append(f"  Agent:   {report.agent_name} v{report.agent_version}")
    lines.append(f"  Model:   {report.model_name}")
    if report.raw_path:
        lines.append(f"  File:    {report.raw_path}")

    lines.append("")
    lines.append("--- Metrics ---")
    lines.append(f"  Steps:        {report.total_steps}")
    lines.append(f"  Agent steps:  {len(report.agent_steps)}")
    lines.append(f"  Tool calls:   {report.tool_call_count}")

    if report.total_tokens is not None:
        lines.append(f"  Tokens:       {report.total_tokens:,}")
        if report.total_prompt_tokens is not None:
            lines.append(f"    Prompt:     {report.total_prompt_tokens:,}")
        if report.total_completion_tokens is not None:
            lines.append(f"    Completion: {report.total_completion_tokens:,}")

    if report.reward is not None:
        pct = report.reward * 100
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        lines.append(f"  Reward:       {pct:.1f}%  [{bar}]")

    _preview_len = 100
    if verbose and report.steps:
        lines.append("")
        lines.append("--- Steps ---")
        for step in report.steps:
            icon = "👤" if step.source == "user" else "🤖"
            preview = step.message[:_preview_len].replace("\n", " ")
            if len(step.message) > _preview_len:
                preview += "..."
            lines.append(f"  {icon} [{step.source}] Step {step.step_id}: {preview}")
            lines.extend(f"    --  {tc.function_name}()" for tc in step.tool_calls)
            if step.observation_count:
                lines.append(f"    -> {step.observation_count} observation(s)")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_tool_usage(report: TrajectoryReport) -> str:
    """Render a ranked tool-usage breakdown.

    Args:
        report: Parsed trajectory.

    Returns:
        Multi-line string with tool call counts.
    """
    counts: dict[str, int] = {}
    for step in report.steps:
        for tc in step.tool_calls:
            counts[tc.function_name] = counts.get(tc.function_name, 0) + 1

    if not counts:
        return "No tool calls recorded."

    ranked = sorted(counts.items(), key=lambda x: -x[1])
    lines = ["Tool usage:"]
    for name, count in ranked:
        lines.append(f"  {count:>4}x  {name}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def find_trajectories(directory: str | Path, *, limit: int = 20) -> list[Path]:
    """Find all ``trajectory.json`` files under *directory*.

    Args:
        directory: Root directory to search.
        limit: Maximum number of results (most recent first by mtime).

    Returns:
        Sorted list of paths (newest first).
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    all_files = list(root.rglob("trajectory.json"))
    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return all_files[:limit]


def load_all_trajectories(
    directory: str | Path,
    *,
    limit: int = 20,
) -> list[TrajectoryReport]:
    """Load all trajectories found under *directory*.

    Parse errors are logged and skipped.

    Args:
        directory: Root directory to search.
        limit: Maximum number of trajectories.

    Returns:
        List of parsed :class:`TrajectoryReport` objects.
    """
    reports = []
    for path in find_trajectories(directory, limit=limit):
        try:
            reports.append(load_trajectory(path))
        except (FileNotFoundError, ValueError):
            logger.warning("Skipping invalid trajectory file: %s", path, exc_info=True)
    return reports
