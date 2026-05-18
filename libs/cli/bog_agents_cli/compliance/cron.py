"""Daemon-facing cron entrypoint (Wave R, R5).

The daemon recipe ``compliance/examples/audit-nightly.yaml`` points at
:func:`run` here. It's intentionally a *small* surface so the daemon's
job-dispatch layer doesn't have to know anything about audit packs.

Contract
--------

The function:

1. Resolves the pack path (a project-local path or a bundled
   example name).
2. Calls :func:`bog_agents_cli.compliance.runner.run_audit`.
3. Persists the sealed markdown + JSON sidecar under
   ``<working_dir>/.bog-agents/audits/<stamp>-<pack>.md``.
4. Returns a small ``CronOutcome`` the daemon dispatch can render
   into its channels (Slack, email, etc.).
5. When ``fail_on_non_pass=True`` (the default), raises
   :class:`CronAuditFailed` on non-PASS so the daemon's exit-code
   policy fires.

The function never touches the TUI, never asks for user input, and
is safe to call from a worker thread.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bog_agents_cli.compliance.audit_pack import (
    PackParseError,
    load_pack_from_yaml,
)
from bog_agents_cli.compliance.evidence import load_trace_slice
from bog_agents_cli.compliance.report import (
    AuditReport,
    Verdict,
    render_markdown,
    report_to_json,
    seal_report,
)
from bog_agents_cli.compliance.runner import run_audit
from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)


class CronAuditFailed(RuntimeError):
    """Raised by :func:`run` when the audit overall verdict isn't PASS
    and ``fail_on_non_pass`` is True.
    """


@dataclass(frozen=True, slots=True)
class CronOutcome:
    """The daemon's view of one audit run."""

    pack_name: str
    overall: Verdict
    counts: dict[Verdict, int]
    saved_markdown: Path
    saved_json: Path
    duration_seconds: float


def run(
    *,
    working_dir: Path | str,
    pack: str,
    fail_on_non_pass: bool = True,
) -> CronOutcome:
    """Daemon entrypoint. See module docstring for the contract.

    Args:
        working_dir: The project root (the daemon passes its
            per-job CWD).
        pack: A path to a pack YAML *or* a bundled example name
            (``soc2-baseline.yaml``).
        fail_on_non_pass: Raise :class:`CronAuditFailed` when the
            overall verdict isn't PASS. Default True so the daemon's
            non-zero exit-code policy can fire.

    Raises:
        FileNotFoundError: When *pack* can't be resolved.
        PackParseError: When the pack YAML is malformed.
        CronAuditFailed: When ``fail_on_non_pass`` is True and the
            audit didn't all-pass.
    """
    started = time.time()
    wdir = Path(working_dir)
    pack_path = _resolve_pack(pack, wdir)
    audit_pack = load_pack_from_yaml(pack_path)
    report = run_audit(audit_pack, working_dir=wdir, now=started)
    saved_md, saved_json = _persist_report(report, audit_pack, wdir)
    finished = time.time()
    outcome = CronOutcome(
        pack_name=report.pack_name,
        overall=report.overall,
        counts=report.counts,
        saved_markdown=saved_md,
        saved_json=saved_json,
        duration_seconds=finished - started,
    )
    logger.info(
        "compliance.cron: pack=%s overall=%s pass=%d fail=%d "
        "inconclusive=%d na=%d md=%s",
        outcome.pack_name,
        outcome.overall.value,
        outcome.counts[Verdict.PASS],
        outcome.counts[Verdict.FAIL],
        outcome.counts[Verdict.INCONCLUSIVE],
        outcome.counts[Verdict.NOT_APPLICABLE],
        saved_md,
    )
    if fail_on_non_pass and outcome.overall != Verdict.PASS:
        msg = (
            f"Audit '{outcome.pack_name}' did not all-pass "
            f"(overall={outcome.overall.value}). "
            f"See {saved_md}."
        )
        raise CronAuditFailed(msg)
    return outcome


def _resolve_pack(pack: str, working_dir: Path) -> Path:
    """Find the pack file: explicit path, project-local, or bundled."""
    candidate = Path(pack)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    # Project-local: <cwd>/audit_packs/<pack> or <cwd>/<pack>
    for prefix in (
        working_dir / "audit_packs" / pack,
        working_dir / pack,
    ):
        if prefix.is_file():
            return prefix
    # Bundled examples
    bundled = Path(__file__).parent / "examples" / pack
    if bundled.is_file():
        return bundled
    msg = (
        f"Could not resolve audit pack {pack!r}. Tried: "
        f"{working_dir / 'audit_packs' / pack}, "
        f"{working_dir / pack}, {bundled}."
    )
    raise FileNotFoundError(msg)


def _persist_report(
    report: AuditReport,
    pack_obj: Any,  # AuditPack — typed Any here to avoid an import cycle
    working_dir: Path,
) -> tuple[Path, Path]:
    """Render + seal the report and write both the markdown and the JSON."""
    by_id = _build_event_index(report, working_dir)
    body = render_markdown(report, pack=pack_obj, by_id=by_id)
    sealed = seal_report(body)
    target_dir = working_dir / ".bog-agents" / "audits"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"{stamp}-{report.pack_name}"
    md_path = target_dir / f"{base}.md"
    json_path = target_dir / f"{base}.json"
    atomic_write_text(md_path, sealed, encoding="utf-8")
    atomic_write_text(json_path, report_to_json(report), encoding="utf-8")
    return md_path, json_path


def _build_event_index(report: AuditReport, working_dir: Path) -> dict[int, object]:
    """Rebuild the event id → event map for sample resolution."""
    slice_ = load_trace_slice(working_dir, report.window)
    return {e.id: e for e in slice_.events}


# Module-level handle to silence the unused-import lint on PackParseError;
# we re-export it via __all__ so daemon code can catch it specifically.
_ = PackParseError


__all__ = ["CronAuditFailed", "CronOutcome", "run"]
