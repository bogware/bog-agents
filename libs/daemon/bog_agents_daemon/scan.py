"""Scan jobs (ROADMAP #59): a profile prompt in, ledger rows out.

A job with `scan_profile` set runs a scan prompt (security / cleanup / perf, or
`custom` with the job's own `prompt` as the rubric) that asks the model to end
with a `## Findings` section in the ledger's line format. After the run the
output is parsed into the SDK `FindingsStore` beside the scanned repo
(`<working_dir>/.bog-agents/findings.db`, or `findings_db`), keyed by stable
fingerprint so a re-scan updates instead of duplicating, findings that stopped
appearing are marked `fixed`, and an optional `scan_gate` severity turns the run
red when open findings sit at or above it. The `--max-cost` in the roadmap is
the job's existing `budget_usd`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents.findings_store import (
    FINDINGS_FORMAT_INSTRUCTIONS,
    SEVERITIES,
    FindingsStore,
    GateResult,
    parse_findings_text,
)

if TYPE_CHECKING:
    from bog_agents_daemon.models import AmbientJob, JobRun

logger = logging.getLogger(__name__)

SCAN_PROFILES: dict[str, str] = {
    "security": (
        "You are running a scheduled security scan of the repository in the working directory.\n"
        "1. Map the architecture: entry points, trust boundaries, where untrusted input enters.\n"
        "2. Hunt for injection, broken authorization, secrets in code or config, SSRF, unsafe deserialization,\n"
        "   path traversal and unsafe subprocess use. Read the code — do not guess from file names.\n"
        "3. For each candidate, confirm it from the source before reporting it; drop anything you cannot confirm.\n"
        "Do NOT modify files."
    ),
    "cleanup": (
        "You are running a scheduled code-health scan of the repository in the working directory.\n"
        "Look for dead code, duplicated logic, TODO/FIXME debt older than the surrounding code, unused\n"
        "dependencies, and tests that assert nothing. Confirm each from the source. Do NOT modify files."
    ),
    "perf": (
        "You are running a scheduled performance scan of the repository in the working directory.\n"
        "Look for N+1 queries, quadratic loops over unbounded input, blocking I/O on async paths, missing\n"
        "indexes or caches on hot paths, and allocations in tight loops. Confirm each from the source.\n"
        "Do NOT modify files."
    ),
    "custom": "Scan the repository in the working directory against this rubric. Confirm every finding from the source.\n\n{rubric}",
}


def scan_prompt(job: AmbientJob) -> str:
    """The prompt for a scan job: the profile text plus the ledger's output format.

    Raises:
        ValueError: For an unknown profile, or `custom` without a rubric in `prompt`.
    """
    profile = job.scan_profile.strip().lower()
    template = SCAN_PROFILES.get(profile)
    if template is None:
        msg = f"Job '{job.name}' has unknown scan_profile {job.scan_profile!r}; use one of {', '.join(SCAN_PROFILES)}"
        raise ValueError(msg)
    if profile == "custom":
        if not job.prompt.strip():
            msg = f"Job '{job.name}' uses scan_profile=custom but has no prompt (the rubric)"
            raise ValueError(msg)
        template = template.format(rubric=job.prompt.strip())
    return f"{template}\n\n{FINDINGS_FORMAT_INSTRUCTIONS}"


def findings_db_path(job: AmbientJob) -> Path:
    """Where this job's ledger lives: `findings_db`, else `.bog-agents/findings.db` under the working dir."""
    if job.findings_db:
        return Path(job.findings_db).expanduser()
    root = Path(job.working_dir).expanduser() if job.working_dir else Path.cwd()
    return root / ".bog-agents" / "findings.db"


@dataclass
class ScanSummary:
    """What one scan run did to the ledger."""

    new: int
    updated: int
    reopened: int
    fixed: int
    open_total: int
    gate: GateResult | None

    def describe(self) -> str:
        """Two lines for the run output / dispatch targets."""
        line = f"Findings ledger: {self.new} new, {self.updated} updated, {self.reopened} reopened, {self.fixed} fixed; {self.open_total} open"
        if self.gate is not None:
            line += "\n" + self.gate.describe()
        return line


def record_scan_output(job: AmbientJob, run: JobRun) -> ScanSummary:
    """Parse the run's output into the job's ledger and apply the gate.

    `source` is `scan:<job name>` so `resolve_missing` only closes findings this
    job reported before, never another job's rows in the same ledger.
    """
    source = f"scan:{job.name or job.job_id}"
    findings = parse_findings_text(run.output, source=source, run_id=run.run_id)
    store = FindingsStore(findings_db_path(job))
    try:
        new, updated, reopened = store.record(findings, run_id=run.run_id)
        fixed = store.resolve_missing(source=source, run_id=run.run_id)
        open_total = len(store.list(states=("open", "triaged")))
        gate = None
        threshold = job.scan_gate.strip().lower()
        if threshold:
            if threshold not in SEVERITIES:
                logger.warning("Job %s scan_gate %r is not a severity; using 'high'", job.job_id, job.scan_gate)
                threshold = "high"
            gate = store.gate(max_severity=threshold)
    finally:
        store.close()
    return ScanSummary(new=new, updated=updated, reopened=reopened, fixed=fixed, open_total=open_total, gate=gate)


__all__ = ["SCAN_PROFILES", "ScanSummary", "findings_db_path", "record_scan_output", "scan_prompt"]
