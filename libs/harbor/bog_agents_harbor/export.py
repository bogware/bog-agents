"""Export Harbor trajectory reports to external destinations.

Supports CSV, JSON, LangSmith, and Weights & Biases (W&B).  Third-party
libraries (`langsmith`, `wandb`) are imported lazily inside each function so
that the module can be imported without those optional dependencies installed.

Usage::

    from bog_agents_harbor.export import export_to_csv, export_to_json

    result = export_to_csv(reports, "/tmp/results.csv")
    if result.success:
        print(f"Exported {result.exported_count} rows to {result.output_path}")
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bog_agents_harbor.reporter import TrajectoryReport

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    """Outcome of an export operation.

    Attributes:
        destination: Human-readable label for the export target (e.g. "csv", "langsmith").
        exported_count: Number of trajectories successfully exported.
        errors: List of error messages from failed exports.
        output_path: File system path written to, if applicable.
    """

    destination: str
    exported_count: int
    errors: list[str] = field(default_factory=list)
    output_path: str | None = None

    @property
    def success(self) -> bool:
        """True when no errors were recorded.

        Returns:
            Whether the export completed without errors.
        """
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "session_id",
    "agent_name",
    "agent_version",
    "model_name",
    "total_steps",
    "tool_call_count",
    "total_tokens",
    "total_prompt_tokens",
    "total_completion_tokens",
    "reward",
]


def _report_to_dict(report: TrajectoryReport) -> dict[str, object]:
    """Serialise a `TrajectoryReport` to a flat dict using the canonical field set.

    Args:
        report: Trajectory to serialise.

    Returns:
        Flat dictionary with the columns defined in `_CSV_FIELDS`.
    """
    return {
        "session_id": report.session_id,
        "agent_name": report.agent_name,
        "agent_version": report.agent_version,
        "model_name": report.model_name,
        "total_steps": report.total_steps,
        "tool_call_count": report.tool_call_count,
        "total_tokens": report.total_tokens,
        "total_prompt_tokens": report.total_prompt_tokens,
        "total_completion_tokens": report.total_completion_tokens,
        "reward": report.reward,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def export_to_csv(reports: list[TrajectoryReport], output_path: str | Path) -> ExportResult:
    """Write trajectory metrics to a CSV file.

    Columns: session_id, agent_name, agent_version, model_name, total_steps,
    tool_call_count, total_tokens, total_prompt_tokens, total_completion_tokens,
    reward.

    Args:
        reports: List of trajectories to export.
        output_path: Destination file path.  Parent directory must exist.

    Returns:
        `ExportResult` describing the outcome.
    """
    dest = str(output_path)
    errors: list[str] = []
    exported = 0

    try:
        with Path(output_path).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
            writer.writeheader()
            for report in reports:
                try:
                    writer.writerow(_report_to_dict(report))
                    exported += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"session {report.session_id}: {exc}")
    except OSError as exc:
        errors.append(f"Could not open {dest}: {exc}")

    return ExportResult(destination="csv", exported_count=exported, errors=errors, output_path=dest if not errors or exported > 0 else None)


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


def export_to_json(reports: list[TrajectoryReport], output_path: str | Path) -> ExportResult:
    """Write trajectory metrics to a JSON file as an array of objects.

    Each object contains the same fields as the CSV export.

    Args:
        reports: List of trajectories to export.
        output_path: Destination file path.  Parent directory must exist.

    Returns:
        `ExportResult` describing the outcome.
    """
    dest = str(output_path)
    errors: list[str] = []
    rows: list[dict[str, object]] = []

    for report in reports:
        try:
            rows.append(_report_to_dict(report))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"session {report.session_id}: {exc}")

    exported = len(rows)
    try:
        Path(output_path).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        errors.append(f"Could not write {dest}: {exc}")
        exported = 0

    return ExportResult(destination="json", exported_count=exported, errors=errors, output_path=dest if exported > 0 else None)


# ---------------------------------------------------------------------------
# LangSmith export
# ---------------------------------------------------------------------------


def export_to_langsmith(
    reports: list[TrajectoryReport],
    *,
    project_name: str = "bog-agents-evals",
    api_key: str | None = None,
) -> ExportResult:
    """Upload trajectory metrics to LangSmith as chain runs.

    Requires the `langsmith` package to be installed.  If it is not available
    the export fails gracefully with an informative error.

    Args:
        reports: List of trajectories to upload.
        project_name: LangSmith project to write runs into.
        api_key: LangSmith API key.  Falls back to the ``LANGCHAIN_API_KEY``
            environment variable when None.

    Returns:
        `ExportResult` describing the outcome.
    """
    errors: list[str] = []
    exported = 0

    try:
        import langsmith  # noqa: PLC0415
    except ImportError:
        return ExportResult(
            destination="langsmith",
            exported_count=0,
            errors=["langsmith package is not installed. Run: pip install langsmith"],
        )

    try:
        client = langsmith.Client(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        return ExportResult(destination="langsmith", exported_count=0, errors=[f"Failed to create LangSmith client: {exc}"])

    for report in reports:
        try:
            client.create_run(
                name=report.session_id,
                run_type="chain",
                inputs={"prompt": "eval"},
                outputs={"reward": report.reward, "steps": report.total_steps},
                project_name=project_name,
            )
            exported += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"session {report.session_id}: {exc}")

    return ExportResult(destination="langsmith", exported_count=exported, errors=errors)


# ---------------------------------------------------------------------------
# W&B export
# ---------------------------------------------------------------------------


def export_to_wandb(
    reports: list[TrajectoryReport],
    *,
    project_name: str = "bog-agents-evals",
    api_key: str | None = None,
) -> ExportResult:
    """Log trajectory metrics to Weights & Biases.

    Each trajectory is logged as a separate W&B run.  Requires the `wandb`
    package to be installed.  If it is not available the export fails
    gracefully with an informative error.

    Args:
        reports: List of trajectories to upload.
        project_name: W&B project name.
        api_key: W&B API key.  When provided, passed to ``wandb.login`` before
            initialising runs.

    Returns:
        `ExportResult` describing the outcome.
    """
    errors: list[str] = []
    exported = 0

    try:
        import wandb  # noqa: PLC0415
    except ImportError:
        return ExportResult(
            destination="wandb",
            exported_count=0,
            errors=["wandb package is not installed. Run: pip install wandb"],
        )

    if api_key is not None:
        try:
            wandb.login(key=api_key, relogin=True)
        except Exception as exc:  # noqa: BLE001
            return ExportResult(destination="wandb", exported_count=0, errors=[f"wandb login failed: {exc}"])

    for report in reports:
        try:
            run = wandb.init(project=project_name, reinit=True)
            run.log(
                {
                    "reward": report.reward,
                    "steps": report.total_steps,
                    "tokens": report.total_tokens,
                    "tool_calls": report.tool_call_count,
                }
            )
            run.finish()
            exported += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"session {report.session_id}: {exc}")

    return ExportResult(destination="wandb", exported_count=exported, errors=errors)
