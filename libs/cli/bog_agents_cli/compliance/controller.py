"""``/audit`` slash-command controller (Wave R, R5).

Wires the audit runner to the TUI. The dispatch surface is:

* ``/audit run <pack-path>`` — run the named pack now, save a
  sealed markdown report under
  ``<cwd>/.bog-agents/audits/<stamp>-<pack>.md`` + a JSON sidecar.
* ``/audit list`` — enumerate saved audit reports newest-first.
* ``/audit show <filename>`` — print a stored report (verifies the
  seal on the way in).
* ``/audit packs`` — list audit packs found under
  ``<cwd>/audit_packs/*.yaml`` *and* the bundled examples shipped
  with this CLI.
* ``/audit help`` — usage.

Reports + their JSON sidecars are intentionally written under a
project-local directory rather than `~/.bog-agents/` so the audit
trail can be checked in alongside the code that produced it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from bog_agents_cli.causal.ledger import CausalEvent
from bog_agents_cli.compliance.audit_pack import (
    PackParseError,
    load_pack_from_yaml,
)
from bog_agents_cli.compliance.evidence import load_trace_slice
from bog_agents_cli.compliance.report import (
    AuditReport,
    render_markdown,
    report_to_json,
    seal_report,
    verify_seal,
)
from bog_agents_cli.compliance.runner import run_audit
from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)


_AUDITS_SUBDIR = ".bog-agents/audits"
_PACK_LOCAL_DIR = "audit_packs"


def dispatch(command_text: str, working_dir: Path | str) -> str:
    """Top-level ``/compliance …`` (and legacy ``/audit …``) handler."""
    rest = command_text.strip()
    for prefix in ("/compliance", "/audit"):
        if rest.startswith(prefix):
            rest = rest[len(prefix) :].strip()
            break
    if not rest or rest.lower() in ("help", "?"):
        return _help_text()
    head, _, tail = rest.partition(" ")
    head = head.lower()
    tail = tail.strip()
    wdir = Path(working_dir)
    if head == "run":
        return _run(tail, wdir)
    if head in ("list", "ls"):
        return _list(wdir)
    if head == "show":
        return _show(tail, wdir)
    if head == "packs":
        return _packs(wdir)
    return f"Unknown /compliance subcommand: {head!r}. Try /compliance help."


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _run(tail: str, working_dir: Path) -> str:
    if not tail:
        return "Usage: /audit run <pack-path>"
    pack_path = (
        (working_dir / tail).resolve() if not Path(tail).is_absolute() else Path(tail)
    )
    if not pack_path.is_file():
        # Try the bundled examples directory.
        examples = _examples_dir()
        candidate = examples / tail
        if candidate.is_file():
            pack_path = candidate
        else:
            return (
                f"Pack file not found: {pack_path}. "
                "Try /audit packs to see available packs."
            )
    try:
        pack = load_pack_from_yaml(pack_path)
    except PackParseError as exc:
        return f"Could not parse audit pack: {exc}"

    report = run_audit(pack, working_dir=working_dir)
    by_id = _build_event_index(report, working_dir)
    body = render_markdown(report, pack=pack, by_id=by_id)
    sealed = seal_report(body)
    saved_md, saved_json = _persist(report, sealed, working_dir)
    return _render_run_summary(report, saved_md, saved_json)


def _list(working_dir: Path) -> str:
    target_dir = working_dir / _AUDITS_SUBDIR
    if not target_dir.is_dir():
        return "No audits saved yet.\nRun /audit run <pack> to create one."
    files = sorted(target_dir.glob("*.md"), reverse=True)
    if not files:
        return f"No audit files under {target_dir}."
    lines = [f"{len(files)} audit report(s):", ""]
    for path in files[:20]:
        lines.append(f"  {path.name}")
    if len(files) > 20:
        lines.append(f"  …and {len(files) - 20} older")
    lines.append("")
    lines.append(f"Directory: {target_dir}")
    return "\n".join(lines)


def _show(tail: str, working_dir: Path) -> str:
    if not tail:
        return "Usage: /audit show <filename>"
    path = (
        working_dir / _AUDITS_SUBDIR / tail
        if not Path(tail).is_absolute()
        else Path(tail)
    )
    if not path.is_file():
        return f"Audit report not found: {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read {path}: {exc}"
    ok, message = verify_seal(text)
    seal_line = f"\n[seal: {'OK' if ok else 'INVALID'} — {message}]\n"
    return text + seal_line


def _packs(working_dir: Path) -> str:
    """Show audit packs in both the project dir and the bundled examples."""
    lines: list[str] = []
    local = working_dir / _PACK_LOCAL_DIR
    if local.is_dir():
        files = sorted(local.glob("*.yaml")) + sorted(local.glob("*.yml"))
        if files:
            lines.append(f"Local packs under {local.name}/:")
            for path in files:
                lines.append(f"  {path.name}")
            lines.append("")
    examples = _examples_dir()
    if examples.is_dir():
        files = sorted(examples.glob("*.yaml")) + sorted(examples.glob("*.yml"))
        if files:
            lines.append("Bundled example packs:")
            for path in files:
                lines.append(f"  {path.name}  ({path})")
            lines.append("")
    if not lines:
        return (
            "No audit packs found.\n"
            "Add a YAML pack under audit_packs/ in your project, "
            "or use a bundled example."
        )
    lines.append("Run: /audit run <pack-path-or-filename>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _help_text() -> str:
    return (
        "/compliance — compliance audit runner.\n\n"
        "Usage:\n"
        "  /compliance run <pack-path>     — run a pack now\n"
        "  /compliance list                — list saved reports\n"
        "  /compliance show <filename>     — read a report; verifies seal\n"
        "  /compliance packs               — list available packs\n"
        "  /compliance help                — this message\n\n"
        "Reports land in .bog-agents/audits/ with a tamper-evident\n"
        "HMAC-SHA-256 seal. Wire to your daemon for nightly cron.\n"
    )


def _persist(report: AuditReport, sealed: str, working_dir: Path) -> tuple[Path, Path]:
    """Write the sealed markdown + JSON sidecar; return both paths."""
    target_dir = working_dir / _AUDITS_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"{stamp}-{report.pack_name}"
    md_path = target_dir / f"{base}.md"
    json_path = target_dir / f"{base}.json"
    atomic_write_text(md_path, sealed, encoding="utf-8")
    atomic_write_text(json_path, report_to_json(report), encoding="utf-8")
    return md_path, json_path


def _build_event_index(
    report: AuditReport,
    working_dir: Path,
) -> dict[int, CausalEvent]:
    """Re-load the trace slice so the renderer can resolve sample ids."""
    slice_ = load_trace_slice(working_dir, report.window)
    return {e.id: e for e in slice_.events}


def _render_run_summary(report: AuditReport, saved_md: Path, saved_json: Path) -> str:
    counts = report.counts
    from bog_agents_cli.compliance.report import _VERDICT_ICON, Verdict

    lines = [
        f"== Audit complete: {report.pack_name} (v{report.pack_version}) ==",
        f"  Overall:  {_VERDICT_ICON[report.overall]} {report.overall.value.upper()}",
        f"  Pass:     {counts[Verdict.PASS]}",
        f"  Fail:     {counts[Verdict.FAIL]}",
        f"  Inconcl.: {counts[Verdict.INCONCLUSIVE]}",
        f"  N/A:      {counts[Verdict.NOT_APPLICABLE]}",
        f"  Sessions: {len(report.sessions_audited)} in window",
        "",
        f"  Saved markdown: {saved_md}",
        f"  Saved JSON:     {saved_json}",
    ]
    if counts[Verdict.FAIL]:
        lines.append("")
        lines.append("Failing checks:")
        for r in report.results:
            if r.verdict == Verdict.FAIL:
                lines.append(f"  ✗ {r.check_id} — {r.title}")
    return "\n".join(lines)


def _examples_dir() -> Path:
    return Path(__file__).parent / "examples"


__all__ = ["dispatch"]
