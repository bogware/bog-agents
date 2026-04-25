"""Side-by-side trajectory comparison for Harbor evaluation runs.

Provides utilities to compare two individual trajectories head-to-head and to
rank a collection of trajectories into a leaderboard.

Usage::

    from bog_agents_harbor.compare import compare_trajectories, format_comparison

    comparison = compare_trajectories(report_a, report_b)
    print(format_comparison(comparison))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bog_agents_harbor.reporter import TrajectoryReport

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TrajectoryComparison:
    """Side-by-side metrics for two trajectory runs.

    Attributes:
        label_a: Display name for the first trajectory.
        label_b: Display name for the second trajectory.
        reward_a: Reward score for run A, or None if not recorded.
        reward_b: Reward score for run B, or None if not recorded.
        steps_a: Total step count for run A.
        steps_b: Total step count for run B.
        tokens_a: Total token count for run A, or None if not recorded.
        tokens_b: Total token count for run B, or None if not recorded.
        tool_count_a: Number of tool calls in run A.
        tool_count_b: Number of tool calls in run B.
        tool_usage_a: Per-tool call counts for run A.
        tool_usage_b: Per-tool call counts for run B.
    """

    label_a: str
    label_b: str
    reward_a: float | None
    reward_b: float | None
    steps_a: int
    steps_b: int
    tokens_a: int | None
    tokens_b: int | None
    tool_count_a: int
    tool_count_b: int
    tool_usage_a: dict[str, int] = field(default_factory=dict)
    tool_usage_b: dict[str, int] = field(default_factory=dict)

    @property
    def winner(self) -> str:
        """Determine the higher-scoring run.

        Compares reward scores: whichever run has the higher reward wins.
        Returns "tie" when both rewards are None or numerically equal.

        Returns:
            "A", "B", or "tie".
        """
        if self.reward_a is None and self.reward_b is None:
            return "tie"
        if self.reward_a is None:
            return "B"
        if self.reward_b is None:
            return "A"
        if self.reward_a > self.reward_b:
            return "A"
        if self.reward_b > self.reward_a:
            return "B"
        return "tie"


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def _tool_usage(report: TrajectoryReport) -> dict[str, int]:
    """Count tool calls grouped by function name across all steps.

    Args:
        report: Trajectory to analyse.

    Returns:
        Mapping of tool function name to call count.
    """
    counts: dict[str, int] = {}
    for step in report.steps:
        for tc in step.tool_calls:
            counts[tc.function_name] = counts.get(tc.function_name, 0) + 1
    return counts


def compare_trajectories(
    report_a: TrajectoryReport,
    report_b: TrajectoryReport,
    *,
    label_a: str = "",
    label_b: str = "",
) -> TrajectoryComparison:
    """Create a side-by-side `TrajectoryComparison` for two runs.

    Labels default to the respective `model_name` fields when not provided.

    Args:
        report_a: First trajectory report.
        report_b: Second trajectory report.
        label_a: Optional display label for run A.
        label_b: Optional display label for run B.

    Returns:
        `TrajectoryComparison` populated with metrics from both runs.
    """
    return TrajectoryComparison(
        label_a=label_a or report_a.model_name,
        label_b=label_b or report_b.model_name,
        reward_a=report_a.reward,
        reward_b=report_b.reward,
        steps_a=report_a.total_steps,
        steps_b=report_b.total_steps,
        tokens_a=report_a.total_tokens,
        tokens_b=report_b.total_tokens,
        tool_count_a=report_a.tool_call_count,
        tool_count_b=report_b.tool_call_count,
        tool_usage_a=_tool_usage(report_a),
        tool_usage_b=_tool_usage(report_b),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_reward(value: float | None) -> str:
    """Format reward as a percentage string, or 'N/A'."""
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _fmt_tokens(value: int | None) -> str:
    """Format token count with thousands separator, or 'N/A'."""
    if value is None:
        return "N/A"
    return f"{value:,}"


def format_comparison(comparison: TrajectoryComparison) -> str:
    """Render a side-by-side comparison table as plain text.

    The winning side for each metric is indicated with a ``>`` marker.
    A tool-usage diff section shows tools called more by each run.

    Args:
        comparison: Populated `TrajectoryComparison`.

    Returns:
        Multi-line string suitable for console output.
    """
    w = comparison.winner
    col = 22  # column width for each run

    def row(label: str, val_a: str, val_b: str, *, higher_wins: bool = True) -> str:
        """Format a single metric row with a winner marker."""
        try:
            num_a = float(val_a.replace("%", "").replace(",", ""))
            num_b = float(val_b.replace("%", "").replace(",", ""))
            mark_a = " >" if (higher_wins and num_a > num_b) or (not higher_wins and num_a < num_b) else "  "
            mark_b = " >" if (higher_wins and num_b > num_a) or (not higher_wins and num_b < num_a) else "  "
        except ValueError:
            logger.debug("compare: could not parse reward value for row formatting; using blank markers")
            mark_a = mark_b = "  "
        return f"  {label:<18} {val_a:<{col}}{mark_a}  {val_b:<{col}}{mark_b}"

    sep = "=" * 72
    lines: list[str] = []
    lines.append(sep)
    lines.append("Trajectory Comparison")
    lines.append(sep)
    lines.append(f"  {'Metric':<18} {'A: ' + comparison.label_a:<{col + 2}}  {'B: ' + comparison.label_b:<{col + 2}}")
    lines.append("-" * 72)
    lines.append(row("Reward", _fmt_reward(comparison.reward_a), _fmt_reward(comparison.reward_b), higher_wins=True))
    lines.append(
        row("Steps", str(comparison.steps_a), str(comparison.steps_b), higher_wins=False)
    )
    lines.append(
        row("Tokens", _fmt_tokens(comparison.tokens_a), _fmt_tokens(comparison.tokens_b), higher_wins=False)
    )
    lines.append(
        row("Tool calls", str(comparison.tool_count_a), str(comparison.tool_count_b), higher_wins=False)
    )
    lines.append("-" * 72)

    if w == "tie":
        lines.append("  Winner: tie")
    else:
        winning_label = comparison.label_a if w == "A" else comparison.label_b
        lines.append(f"  Winner: {w} ({winning_label})")

    # Tool usage diff
    all_tools = sorted(set(comparison.tool_usage_a) | set(comparison.tool_usage_b))
    if all_tools:
        lines.append("")
        lines.append("--- Tool Usage Diff ---")
        more_a: list[tuple[str, int, int]] = []
        more_b: list[tuple[str, int, int]] = []
        same: list[tuple[str, int]] = []
        for tool in all_tools:
            cnt_a = comparison.tool_usage_a.get(tool, 0)
            cnt_b = comparison.tool_usage_b.get(tool, 0)
            if cnt_a > cnt_b:
                more_a.append((tool, cnt_a, cnt_b))
            elif cnt_b > cnt_a:
                more_b.append((tool, cnt_a, cnt_b))
            else:
                same.append((tool, cnt_a))

        if more_a:
            lines.append(f"  Tools used more by A ({comparison.label_a}):")
            for tool, cnt_a, cnt_b in more_a:
                lines.append(f"    {tool}: {cnt_a} vs {cnt_b}")
        if more_b:
            lines.append(f"  Tools used more by B ({comparison.label_b}):")
            for tool, cnt_a, cnt_b in more_b:
                lines.append(f"    {tool}: {cnt_a} vs {cnt_b}")
        if same:
            lines.append("  Tools used equally:")
            for tool, cnt in same:
                lines.append(f"    {tool}: {cnt}")

    lines.append(sep)
    return "\n".join(lines)


def compare_multiple(reports: list[tuple[str, TrajectoryReport]]) -> str:
    """Rank a collection of trajectories by reward into a leaderboard.

    Runs with no reward are placed at the bottom.

    Args:
        reports: List of (label, report) tuples to rank.

    Returns:
        Multi-line leaderboard string sorted by reward descending.
    """
    if not reports:
        return "No trajectories to compare."

    def sort_key(item: tuple[str, TrajectoryReport]) -> tuple[int, float]:
        reward = item[1].reward
        if reward is None:
            return (1, 0.0)
        return (0, -reward)

    ranked = sorted(reports, key=sort_key)

    sep = "=" * 72
    lines: list[str] = []
    lines.append(sep)
    lines.append("Trajectory Leaderboard")
    lines.append(sep)
    lines.append(f"  {'Rank':<6} {'Label':<28} {'Reward':>8}  {'Steps':>6}  {'Tokens':>10}  {'Tools':>6}")
    lines.append("-" * 72)

    for rank, (label, report) in enumerate(ranked, start=1):
        reward_str = _fmt_reward(report.reward)
        tokens_str = _fmt_tokens(report.total_tokens)
        lines.append(
            f"  {rank:<6} {label:<28} {reward_str:>8}  {report.total_steps:>6}  {tokens_str:>10}  {report.tool_call_count:>6}"
        )

    lines.append(sep)
    return "\n".join(lines)
