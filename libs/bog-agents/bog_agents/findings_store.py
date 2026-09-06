"""Findings ledger (ROADMAP #59 / #70): stable fingerprints, triage states, SARIF, a CI gate.

Scans (a daemon `scan` job, `/jury`, `/self-review`, a security recipe) produce
findings as text; this module turns them into rows keyed by a fingerprint that
survives line shifts (`rule_id` + path + normalised message, never the line
number), so a re-scan updates `last_seen` and `occurrences` instead of
re-opening the same issue, a finding that stops appearing is auto-marked
`fixed`, and a triage state (`triaged` / `wontfix` / `false_positive`) sticks
until the code changes enough to change the message. `to_sarif()` renders the
open set as SARIF 2.1.0 for code-scanning uploads and `gate()` is the yes/no a
CI step wants. SQLite, standard library only, one file per project.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

SEVERITIES: tuple[str, ...] = ("info", "low", "medium", "high", "critical")
STATES: tuple[str, ...] = ("open", "triaged", "fixed", "wontfix", "false_positive")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
_SARIF_LEVEL = {"info": "note", "low": "note", "medium": "warning", "high": "error", "critical": "error"}
_WS = re.compile(r"\s+")
_NUMBERS = re.compile(r"\b\d+\b")
_LINE_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])?\s*(?:\[(?P<sev1>[A-Za-z_]+)\]\s*)?(?P<path>[\w./\\-]+\.[A-Za-z0-9]+):(?P<line>\d+)(?::\d+)?\s*[-—:]?\s*(?:\[(?P<sev2>[A-Za-z_]+)\]\s*)?(?P<msg>.+?)\s*$"
)
_SEV_WORD = re.compile(r"\b(critical|high|medium|low|info)\b", re.IGNORECASE)
FINDINGS_FORMAT_INSTRUCTIONS = (
    "End your answer with a section headed `## Findings` listing every confirmed finding as one line:\n"
    "`<path>:<line> [<severity>] <RULE_ID>: <message>` - severity one of critical / high / medium / low / info;\n"
    "RULE_ID an upper-case token naming the class (SQLI, AUTHZ, SECRET, SSRF, DESER, TRAVERSAL, INJECT, XSS, LOGIC,\n"
    "PERF, DEAD, DUP, DEBT); message one sentence that reads the same on a re-scan (no counts, dates or line\n"
    "numbers in the text). Write `## Findings` followed by `none` when there is nothing to report."
)


def normalize_message(message: str) -> str:
    """Whitespace-collapsed, number-free, lower-cased message (the fingerprint's text part)."""
    return _WS.sub(" ", _NUMBERS.sub("#", message)).strip().lower()


def fingerprint(rule_id: str, path: str, message: str) -> str:
    """`sha256:<hex>` over rule, path and the normalised message — stable across line shifts."""
    payload = "\x1f".join((rule_id.strip().lower(), path.replace("\\", "/").strip(), normalize_message(message)))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class Finding:
    """One finding, as stored."""

    rule_id: str
    path: str
    message: str
    severity: str = "medium"
    line: int = 0
    source: str = ""
    fingerprint: str = ""
    state: str = "open"
    note: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    occurrences: int = 1
    run_id: str = ""

    def __post_init__(self) -> None:
        """Normalise severity / state and compute the fingerprint when absent."""
        self.severity = self.severity.lower() if self.severity.lower() in SEVERITIES else "medium"
        if self.state not in STATES:
            self.state = "open"
        self.path = self.path.replace("\\", "/")
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.rule_id, self.path, self.message)

    @property
    def rank(self) -> int:
        """Numeric severity (higher = worse)."""
        return _SEVERITY_RANK[self.severity]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return asdict(self)


def parse_findings_text(text: str, *, source: str = "", default_rule: str = "finding", run_id: str = "") -> list[Finding]:
    """Parse `path:line [SEV] message` style lines (bullets and numbering tolerated) into findings.

    Lines without a `path:line` are ignored, so a review's prose does not
    become findings; a severity word anywhere on the line is honoured, else
    `medium`.
    """
    findings: list[Finding] = []
    for raw in text.splitlines():
        match = _LINE_RE.match(raw)
        if not match:
            continue
        message = match.group("msg").strip()
        sev = (match.group("sev1") or match.group("sev2") or "").lower()
        if sev not in SEVERITIES:
            word = _SEV_WORD.search(message)
            sev = word.group(1).lower() if word else "medium"
        rule_id = default_rule
        rule_match = re.match(r"^(?P<rule>[A-Z][A-Z0-9_-]{1,40}):\s*(?P<rest>.+)$", message)
        if rule_match:
            rule_id = rule_match.group("rule")
            message = rule_match.group("rest")
        findings.append(
            Finding(
                rule_id=rule_id, path=match.group("path"), line=int(match.group("line")), message=message, severity=sev, source=source, run_id=run_id
            )
        )
    return findings


@dataclass
class GateResult:
    """Outcome of `gate()`."""

    passed: bool
    blocking: list[Finding]
    threshold: str

    def describe(self) -> str:
        """One line for CI."""
        if self.passed:
            return f"findings gate passed (no open findings at or above {self.threshold})"
        worst = max(f.rank for f in self.blocking)
        return f"findings gate FAILED: {len(self.blocking)} open finding(s) at or above {self.threshold} (worst: {SEVERITIES[worst]})"


class FindingsStore:
    """SQLite ledger of findings keyed by fingerprint."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        """Open (creating if needed) the ledger."""
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS findings ("
                " fingerprint TEXT PRIMARY KEY, rule_id TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL DEFAULT 0,"
                " message TEXT NOT NULL, severity TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'open',"
                " note TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL, last_seen REAL NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,"
                " run_id TEXT NOT NULL DEFAULT '')"
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    def _row(self, row: tuple[Any, ...]) -> Finding:
        keys = (
            "fingerprint",
            "rule_id",
            "path",
            "line",
            "message",
            "severity",
            "source",
            "state",
            "note",
            "first_seen",
            "last_seen",
            "occurrences",
            "run_id",
        )
        return Finding(**dict(zip(keys, row, strict=True)))

    def get(self, fp: str) -> Finding | None:
        """One finding by (full or prefix) fingerprint."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM findings WHERE fingerprint = ? OR fingerprint LIKE ? LIMIT 1", (fp, f"{fp}%")).fetchone()
        return self._row(row) if row else None

    def record(self, findings: Iterable[Finding], *, run_id: str = "", now: float | None = None) -> tuple[int, int, int]:
        """Upsert findings from one run; returns `(new, updated, reopened)`.

        An existing `fixed` finding that shows up again is reopened; `wontfix`
        and `false_positive` keep their state (the note survives too).
        """
        ts = time.time() if now is None else now
        new = updated = reopened = 0
        with self._lock:
            for finding in findings:
                row = self._conn.execute("SELECT state, occurrences FROM findings WHERE fingerprint = ?", (finding.fingerprint,)).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO findings (fingerprint, rule_id, path, line, message, severity, source, state, note,"
                        " first_seen, last_seen, occurrences, run_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?, 1, ?)",
                        (
                            finding.fingerprint,
                            finding.rule_id,
                            finding.path,
                            finding.line,
                            finding.message,
                            finding.severity,
                            finding.source,
                            ts,
                            ts,
                            run_id,
                        ),
                    )
                    new += 1
                    continue
                state, occurrences = row
                next_state = "open" if state == "fixed" else state
                if state == "fixed":
                    reopened += 1
                else:
                    updated += 1
                self._conn.execute(
                    "UPDATE findings SET line = ?, message = ?, severity = ?, source = ?, state = ?, last_seen = ?,"
                    " occurrences = ?, run_id = ? WHERE fingerprint = ?",
                    (
                        finding.line,
                        finding.message,
                        finding.severity,
                        finding.source,
                        next_state,
                        ts,
                        int(occurrences) + 1,
                        run_id,
                        finding.fingerprint,
                    ),
                )
            self._conn.commit()
        return new, updated, reopened

    def resolve_missing(self, *, source: str, run_id: str) -> int:
        """Mark `open` / `triaged` findings from `source` that this run did not report as `fixed`; returns the count."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE findings SET state = 'fixed' WHERE source = ? AND run_id != ? AND state IN ('open', 'triaged')",
                (source, run_id),
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def triage(self, fp: str, state: str, *, note: str = "") -> Finding | None:
        """Set a triage state (and note) on one finding; `None` when unknown.

        Raises:
            ValueError: For a state outside `STATES`.
        """
        if state not in STATES:
            msg = f"unknown state {state!r}; use one of {', '.join(STATES)}"
            raise ValueError(msg)
        found = self.get(fp)
        if found is None:
            return None
        with self._lock:
            self._conn.execute("UPDATE findings SET state = ?, note = ? WHERE fingerprint = ?", (state, note or found.note, found.fingerprint))
            self._conn.commit()
        return self.get(found.fingerprint)

    def list(self, *, states: Iterable[str] | None = None, min_severity: str = "info", source: str | None = None) -> list[Finding]:
        """Findings, worst first then newest; filtered by state / severity floor / source."""
        wanted = tuple(states) if states is not None else None
        with self._lock:
            rows = self._conn.execute("SELECT * FROM findings").fetchall()
        floor = _SEVERITY_RANK.get(min_severity, 0)
        out = [
            f
            for f in (self._row(r) for r in rows)
            if (wanted is None or f.state in wanted) and f.rank >= floor and (source is None or f.source == source)
        ]
        return sorted(out, key=lambda f: (-f.rank, -f.last_seen, f.path, f.line))

    def counts(self) -> dict[str, int]:
        """`{state: count}`."""
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) FROM findings GROUP BY state").fetchall()
        return {str(state): int(count) for state, count in rows}

    def gate(self, *, max_severity: str = "high", states: Iterable[str] = ("open", "triaged")) -> GateResult:
        """Fail when any finding in `states` is at or above `max_severity`."""
        blocking = self.list(states=states, min_severity=max_severity)
        return GateResult(passed=not blocking, blocking=blocking, threshold=max_severity)

    def to_sarif(self, *, tool_name: str = "bog-agents", tool_version: str = "", states: Iterable[str] = ("open", "triaged")) -> dict[str, Any]:
        """SARIF 2.1.0 for the findings in `states` (code-scanning upload shape)."""
        findings = self.list(states=states)
        rules: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        for finding in findings:
            rules.setdefault(finding.rule_id, {"id": finding.rule_id, "shortDescription": {"text": finding.rule_id}})
            results.append(
                {
                    "ruleId": finding.rule_id,
                    "level": _SARIF_LEVEL[finding.severity],
                    "message": {"text": finding.message},
                    "locations": [{"physicalLocation": {"artifactLocation": {"uri": finding.path}, "region": {"startLine": max(1, finding.line)}}}],
                    "partialFingerprints": {"bogFingerprint/v1": finding.fingerprint},
                    "properties": {
                        "severity": finding.severity,
                        "state": finding.state,
                        "source": finding.source,
                        "occurrences": finding.occurrences,
                    },
                }
            )
        driver: dict[str, Any] = {"name": tool_name, "rules": list(rules.values())}
        if tool_version:
            driver["version"] = tool_version
        return {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{"tool": {"driver": driver}, "results": results}],
        }

    def render(self, findings: Iterable[Finding] | None = None, *, limit: int = 50) -> str:
        """A short table for the terminal."""
        rows = list(findings) if findings is not None else self.list(states=("open", "triaged"))
        if not rows:
            return "No open findings."
        lines = [f"{'FINGERPRINT':<14} {'SEV':<8} {'STATE':<14} LOCATION — RULE: MESSAGE"]
        for finding in rows[:limit]:
            where = f"{finding.path}:{finding.line}" if finding.line else finding.path
            lines.append(
                f"{finding.fingerprint[7:19]:<14} {finding.severity:<8} {finding.state:<14} {where} — {finding.rule_id}: {finding.message[:80]}"
            )
        if len(rows) > limit:
            lines.append(f"… {len(rows) - limit} more")
        return "\n".join(lines)


def dump_sarif(store: FindingsStore, path: str | Path, **kwargs: Any) -> Path:
    """Write `to_sarif()` to `path`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(store.to_sarif(**kwargs), indent=2), encoding="utf-8")
    return target


__all__ = [
    "FINDINGS_FORMAT_INSTRUCTIONS",
    "SEVERITIES",
    "STATES",
    "Finding",
    "FindingsStore",
    "GateResult",
    "dump_sarif",
    "fingerprint",
    "normalize_message",
    "parse_findings_text",
]
