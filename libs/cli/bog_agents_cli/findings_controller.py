"""`/findings` and `/remediate` (ROADMAP #59 / #70): the project's findings ledger from the TUI and headless.

The ledger is the SDK `FindingsStore` at `.bog-agents/findings.db` under the
project root — the same file a daemon `scan` job on this repo writes, so a
nightly scan, `/findings record` of a security-scan recipe report, and a CI
`bog-agents command "/findings gate --max high"` all read one set of rows.
`/remediate <fingerprint>` turns one finding into a fix turn for the agent
with the finding's evidence in the prompt; `/pr --evidence` then opens the PR.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.findings_store import (
    SEVERITIES,
    STATES,
    Finding,
    FindingsStore,
    GateResult,
    dump_sarif,
    parse_findings_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

USAGE = (
    "Usage: /findings [list] [--all | --state S] [--min SEV] [--source S] | /findings show <fp> | "
    "/findings triage <fp> <open|triaged|fixed|wontfix|false_positive> [note] | /findings gate [--max SEV] | "
    "/findings sarif [file] | /findings record <report-file> [--source S]"
)
DB_RELATIVE = Path(".bog-agents") / "findings.db"
DEFAULT_GATE = "high"


def project_root(app: Any) -> Path:  # noqa: ANN401 - the App
    """The project root the TUI is working in (settings first, then the app's cwd)."""
    try:
        from bog_agents_cli.config import settings

        root = settings.project_root
    except Exception:
        root = None
    return Path(root) if root else Path(getattr(app, "_cwd", ".") or ".")


def findings_db_path(root: Path) -> Path:
    """`<root>/.bog-agents/findings.db`."""
    return Path(root) / DB_RELATIVE


def open_store(root: Path) -> FindingsStore:
    """Open (creating) the project ledger."""
    return FindingsStore(findings_db_path(root))


def _flag(tokens: list[str], name: str, default: str | None = None) -> str | None:
    """Pop `--name value` from `tokens`."""
    if name in tokens:
        index = tokens.index(name)
        value = tokens[index + 1] if index + 1 < len(tokens) else None
        del tokens[index : index + 2]
        return value
    return default


def _severity(value: str | None, fallback: str) -> str:
    value = (value or "").strip().lower()
    return value if value in SEVERITIES else fallback


def list_findings(
    root: Path,
    *,
    states: Sequence[str] | None = ("open", "triaged"),
    min_severity: str = "info",
    source: str | None = None,
) -> list[Finding]:
    """Rows for the table (worst first)."""
    store = open_store(root)
    try:
        return store.list(states=states, min_severity=min_severity, source=source)
    finally:
        store.close()


def gate_result(root: Path, *, max_severity: str = DEFAULT_GATE) -> GateResult:
    """The CI gate for this project."""
    store = open_store(root)
    try:
        return store.gate(max_severity=max_severity)
    finally:
        store.close()


def record_report(root: Path, text: str, *, source: str, run_id: str = "") -> str:
    """Ingest a `## Findings` report (a recipe's output, a pasted review) into the ledger."""
    findings = parse_findings_text(text, source=source, run_id=run_id)
    store = open_store(root)
    try:
        new, updated, reopened = store.record(findings, run_id=run_id)
        fixed = store.resolve_missing(source=source, run_id=run_id) if run_id else 0
        open_total = len(store.list(states=("open", "triaged")))
    finally:
        store.close()
    if not findings:
        return f"No `path:line [severity] RULE: message` lines found in the report; nothing recorded (source {source!r})."
    return f"Recorded {len(findings)} finding(s) from {source!r}: {new} new, {updated} updated, {reopened} reopened, {fixed} fixed; {open_total} open."


def run_findings_command(command: str, root: Path) -> str:
    """Body of `/findings`."""
    try:
        tokens = shlex.split(command.strip())[1:]
    except ValueError as exc:
        return f"Could not parse arguments: {exc}\n{USAGE}"
    verb = tokens[0].lower() if tokens and not tokens[0].startswith("--") else "list"
    if tokens and not tokens[0].startswith("--"):
        tokens = tokens[1:]
    if verb in {"help", "-h", "--help"}:
        return USAGE
    if verb == "list":
        show_all = "--all" in tokens
        if show_all:
            tokens.remove("--all")
        state = _flag(tokens, "--state")
        min_severity = _severity(_flag(tokens, "--min"), "info")
        source = _flag(tokens, "--source")
        states: Sequence[str] | None = (
            STATES if show_all else ((state,) if state else ("open", "triaged"))
        )
        if state and state not in STATES:
            return f"Unknown state {state!r}; use one of {', '.join(STATES)}."
        rows = list_findings(
            root, states=states, min_severity=min_severity, source=source
        )
        store = open_store(root)
        try:
            counts = store.counts()
            table = store.render(rows)
        finally:
            store.close()
        summary = (
            ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
            or "empty ledger"
        )
        return f"{table}\n\nLedger {findings_db_path(root)}: {summary}."
    if verb == "show":
        if not tokens:
            return USAGE
        store = open_store(root)
        try:
            found = store.get(tokens[0])
        finally:
            store.close()
        if found is None:
            return f"No finding matches {tokens[0]!r}."
        lines = [f"{key}: {value}" for key, value in found.to_dict().items()]
        return "\n".join(lines)
    if verb == "triage":
        if len(tokens) < 2:
            return USAGE
        fp, state, *note_words = tokens
        if state not in STATES:
            return f"Unknown state {state!r}; use one of {', '.join(STATES)}."
        store = open_store(root)
        try:
            updated = store.triage(fp, state, note=" ".join(note_words))
        finally:
            store.close()
        if updated is None:
            return f"No finding matches {fp!r}."
        return f"{updated.fingerprint[7:19]} → {updated.state}" + (
            f" ({updated.note})" if updated.note else ""
        )
    if verb == "gate":
        max_severity = _severity(_flag(tokens, "--max"), DEFAULT_GATE)
        result = gate_result(root, max_severity=max_severity)
        store = open_store(root)
        try:
            body = store.render(result.blocking) if result.blocking else ""
        finally:
            store.close()
        return result.describe() + (f"\n{body}" if body else "")
    if verb == "sarif":
        target = Path(tokens[0]) if tokens else root / ".bog-agents" / "findings.sarif"
        if not target.is_absolute():
            target = root / target
        store = open_store(root)
        try:
            written = dump_sarif(store, target, tool_name="bog-agents")
        finally:
            store.close()
        return f"SARIF written to {written}"
    if verb == "record":
        if not tokens:
            return USAGE
        source = _flag(tokens, "--source", "report") or "report"
        run_id = _flag(tokens, "--run", "") or ""
        report = Path(tokens[0])
        if not report.is_absolute():
            report = root / report
        try:
            text = report.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Cannot read {report}: {exc}"
        return record_report(root, text, source=source, run_id=run_id)
    return f"Unknown verb {verb!r}.\n{USAGE}"


def run_findings_headless(
    args: str, root: Path
) -> tuple[bool, str, dict[str, Any] | None]:
    """`(ok, text, data)` for the headless twin: `gate` is ok only when it passes; `list` / `gate` carry rows."""
    tokens = args.split()
    verb = tokens[0].lower() if tokens and not tokens[0].startswith("--") else "list"
    text = run_findings_command(f"/findings {args}", root)
    if verb == "gate":
        max_severity = _severity(_flag(list(tokens), "--max"), DEFAULT_GATE)
        result = gate_result(root, max_severity=max_severity)
        data: dict[str, Any] = {
            "passed": result.passed,
            "threshold": result.threshold,
            "blocking": len(result.blocking),
            "findings": [f.to_dict() for f in result.blocking],
        }
        return result.passed, text, data
    if verb == "list":
        rest = list(tokens[1:] if tokens and tokens[0].lower() == "list" else tokens)
        show_all = "--all" in rest
        state = _flag(rest, "--state")
        min_severity = _severity(_flag(rest, "--min"), "info")
        source = _flag(rest, "--source")
        states: Sequence[str] | None = (
            STATES
            if show_all
            else ((state,) if state in STATES else ("open", "triaged"))
        )
        rows = list_findings(
            root, states=states, min_severity=min_severity, source=source
        )
        return (
            True,
            text,
            {
                "findings": [f.to_dict() for f in rows],
                "db": str(findings_db_path(root)),
            },
        )
    ok = not text.startswith(
        ("Unknown", "No finding matches", "Cannot read", "Could not parse")
    )
    return ok, text, None


def remediation_prompt(command: str, root: Path) -> tuple[str | None, str]:
    """`(prompt for the agent, note for the user)` for `/remediate <fingerprint>`."""
    tokens = command.strip().split()[1:]
    if not tokens:
        return None, "Usage: /remediate <fingerprint> — pick one from /findings."
    store = open_store(root)
    try:
        found = store.get(tokens[0])
        if found is not None and found.state in {"open", "triaged"}:
            store.triage(
                found.fingerprint, "triaged", note=found.note or "remediation started"
            )
    finally:
        store.close()
    if found is None:
        return None, f"No finding matches {tokens[0]!r}."
    if found.state in {"fixed", "wontfix", "false_positive"}:
        return (
            None,
            f"Finding {found.fingerprint[7:19]} is {found.state}; re-open it with /findings triage {found.fingerprint[7:19]} open first.",
        )
    where = f"{found.path}:{found.line}" if found.line else found.path
    prompt = (
        f"Remediate this finding from the project's findings ledger (fingerprint {found.fingerprint}).\n\n"
        f"- Rule: {found.rule_id}\n- Severity: {found.severity}\n- Location: {where}\n- Finding: {found.message}\n"
        f"- Reported by: {found.source or 'unknown'} (seen {found.occurrences} time(s))\n"
        + (f"- Triage note: {found.note}\n" if found.note else "")
        + "\nSteps: read the code at the location and confirm the finding is real; if it is not, say so and stop. "
        "Otherwise make the smallest fix that removes the root cause, add or adjust a test that would have caught it, "
        "run the project's checks, and end with a short summary (what was wrong, what changed, how it was verified) "
        "suitable for a pull-request body."
    )
    note = f"Remediating {found.fingerprint[7:19]} ({found.severity} {found.rule_id} at {where}); run /pr --evidence when the fix is in."
    return prompt, note


__all__ = [
    "DEFAULT_GATE",
    "USAGE",
    "findings_db_path",
    "gate_result",
    "list_findings",
    "open_store",
    "project_root",
    "record_report",
    "remediation_prompt",
    "run_findings_command",
    "run_findings_headless",
]
