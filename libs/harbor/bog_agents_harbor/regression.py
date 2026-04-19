"""Regression detection for Harbor evaluation runs.

Compares evaluation scores across runs to surface regressions and improvements
relative to a recorded baseline.

Usage::

    from bog_agents_harbor.regression import compute_baseline, detect_regression, format_regression_report

    baseline = compute_baseline(previous_reports)
    result = detect_regression(current_reports, baseline)
    print(format_regression_report(result))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from bog_agents_harbor.reporter import TrajectoryReport

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RegressionResult:
    """Outcome of comparing a set of evaluation scores against a baseline.

    Attributes:
        benchmark_name: Identifier for the benchmark suite being evaluated.
        baseline_score: Mean reward score from the reference run.
        current_score: Mean reward score from the current run.
        delta: Signed difference (current_score - baseline_score).
        threshold: Minimum absolute delta required to declare a regression or improvement.
        is_regression: True when delta < -threshold.
        is_improvement: True when delta > threshold.
        trajectory_count: Number of trajectories in the current run.
    """

    benchmark_name: str
    baseline_score: float
    current_score: float
    delta: float
    threshold: float
    is_regression: bool
    is_improvement: bool
    trajectory_count: int

    @property
    def pct_change(self) -> float:
        """Percentage change relative to baseline.

        Returns:
            Delta expressed as a percentage of the baseline score, or 0.0 when
            baseline_score is zero to avoid division by zero.
        """
        if self.baseline_score == 0.0:
            return 0.0
        return (self.delta / self.baseline_score) * 100.0


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def compute_baseline(reports: list[TrajectoryReport], *, benchmark_name: str = "default") -> float:  # noqa: ARG001
    """Compute a baseline score as the mean reward across *reports*.

    Only reports that carry a non-None reward contribute to the mean.  If no
    report has a reward, 0.0 is returned.

    Args:
        reports: Trajectory reports from a reference run.
        benchmark_name: Human-readable label for the benchmark (unused in
            computation; kept for call-site symmetry with `detect_regression`).

    Returns:
        Mean reward in [0.0, 1.0], or 0.0 when no rewards are available.
    """
    scores: list[float] = []
    for r in reports:
        if r.reward is None:
            continue
        if not 0.0 <= r.reward <= 1.0:
            logger.warning(
                "compute_baseline: reward %.4f for session %s is outside [0, 1]; including anyway",
                r.reward,
                getattr(r, "session_id", "?"),
            )
        scores.append(r.reward)
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def detect_regression(
    current_reports: list[TrajectoryReport],
    baseline_score: float,
    *,
    threshold: float = 0.05,
    benchmark_name: str = "default",
) -> RegressionResult:
    """Detect whether *current_reports* represent a regression against *baseline_score*.

    Args:
        current_reports: Trajectory reports from the run under evaluation.
        baseline_score: Pre-computed reference score (e.g. from `compute_baseline`).
        threshold: Minimum absolute delta for a regression or improvement to be
            declared.  Defaults to 0.05 (5 percentage points on a 0-1 scale).
        benchmark_name: Label attached to the returned `RegressionResult`.

    Returns:
        `RegressionResult` summarising the comparison.
    """
    current_score = compute_baseline(current_reports, benchmark_name=benchmark_name)
    delta = current_score - baseline_score
    return RegressionResult(
        benchmark_name=benchmark_name,
        baseline_score=baseline_score,
        current_score=current_score,
        delta=delta,
        threshold=threshold,
        is_regression=delta < -threshold,
        is_improvement=delta > threshold,
        trajectory_count=len(current_reports),
    )


def compare_runs(
    run_a: list[TrajectoryReport],
    run_b: list[TrajectoryReport],
    *,
    benchmark_name: str = "default",
    threshold: float = 0.05,
) -> RegressionResult:
    """Compare two sets of trajectories, treating *run_a* as the baseline.

    Args:
        run_a: Reference run (baseline).
        run_b: Candidate run (current).
        benchmark_name: Label attached to the returned `RegressionResult`.
        threshold: Minimum absolute delta for regression/improvement classification.

    Returns:
        `RegressionResult` with *run_a* mean as baseline and *run_b* mean as
        current score.
    """
    baseline_score = compute_baseline(run_a, benchmark_name=benchmark_name)
    return detect_regression(run_b, baseline_score, threshold=threshold, benchmark_name=benchmark_name)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_regression_report(result: RegressionResult) -> str:
    """Render a human-readable regression report.

    Args:
        result: The `RegressionResult` to render.

    Returns:
        Multi-line string suitable for console output.
    """
    lines: list[str] = []
    sep = "=" * 60
    lines.append(sep)
    lines.append(f"Regression Report: {result.benchmark_name}")
    lines.append(sep)
    lines.append(f"  Trajectories:  {result.trajectory_count}")
    lines.append(f"  Baseline:      {result.baseline_score:.4f}  ({result.baseline_score * 100:.1f}%)")
    lines.append(f"  Current:       {result.current_score:.4f}  ({result.current_score * 100:.1f}%)")

    sign = "+" if result.delta >= 0 else ""
    lines.append(f"  Delta:         {sign}{result.delta:.4f}  ({sign}{result.pct_change:.1f}%)")
    lines.append(f"  Threshold:     +/-{result.threshold:.4f}")
    lines.append("")

    if result.is_regression:
        lines.append("  Status: REGRESSION  -- score dropped below threshold")
    elif result.is_improvement:
        lines.append("  Status: IMPROVEMENT -- score rose above threshold")
    else:
        lines.append("  Status: NEUTRAL     -- delta within threshold, no significant change")

    lines.append(sep)
    return "\n".join(lines)
