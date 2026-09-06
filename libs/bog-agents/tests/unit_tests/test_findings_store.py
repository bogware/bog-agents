"""ROADMAP #59 / #70: the findings ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bog_agents import findings_store as fs

SCAN = """
Findings:
- src/auth.py:42 [HIGH] SQLI: user input reaches the query
- src/auth.py:88 — [medium] Token compared with == (timing)
2) tests/test_x.py:3: low: unused import, line 3 of 120
This line has no location and is prose.
- lib/util.py:10 Something critical happens here
"""

SECOND = """
- src/auth.py:50 [HIGH] SQLI: user input reaches the query
- src/auth.py:90 — [medium] Token compared with == (timing)
- tests/test_x.py:7: low: unused import, line 7 of 130
"""


def test_parse_findings_text() -> None:
    findings = fs.parse_findings_text(SCAN, source="scan")
    assert [(f.path, f.line, f.severity) for f in findings] == [
        ("src/auth.py", 42, "high"),
        ("src/auth.py", 88, "medium"),
        ("tests/test_x.py", 3, "low"),
        ("lib/util.py", 10, "critical"),
    ]
    assert findings[0].rule_id == "SQLI" and findings[0].message == "user input reaches the query"
    assert findings[1].rule_id == "finding"
    assert all(f.fingerprint.startswith("sha256:") and f.source == "scan" for f in findings)


def test_fingerprint_ignores_line_numbers_and_whitespace() -> None:
    a = fs.fingerprint("R1", "src/a.py", "unused variable at line 12")
    b = fs.fingerprint("R1", "src\\a.py", "  unused  variable at line 99 ")
    assert a == b
    assert fs.fingerprint("R2", "src/a.py", "unused variable") != a


def test_record_upsert_reopen_triage_and_gate(tmp_path: Path) -> None:
    store = fs.FindingsStore(tmp_path / "findings.db")
    first = fs.parse_findings_text(SCAN, source="scan", run_id="r1")
    assert store.record(first, run_id="r1", now=100.0) == (4, 0, 0)
    assert store.counts() == {"open": 4}
    gate = store.gate(max_severity="high")
    assert not gate.passed and len(gate.blocking) == 2 and "FAILED" in gate.describe()

    worst = store.list(min_severity="high")[0]
    assert worst.severity == "critical"
    high = next(f for f in store.list() if f.rule_id == "SQLI")
    assert store.triage(high.fingerprint[:19], "false_positive", note="parameterised upstream") is not None
    assert store.get(high.fingerprint).state == "false_positive"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="unknown state"):
        store.triage(high.fingerprint, "bogus")
    assert store.triage("sha256:nope", "fixed") is None

    # Second run: SQLI (false positive) and the timing issue persist, the critical one is gone,
    # the unused import moved to another line.
    second = fs.parse_findings_text(SECOND, source="scan", run_id="r2")
    assert store.record(second, run_id="r2", now=200.0) == (0, 3, 0)
    assert store.resolve_missing(source="scan", run_id="r2") == 1
    states = {f.rule_id + ":" + f.path: f.state for f in store.list(states=fs.STATES)}
    assert states["SQLI:src/auth.py"] == "false_positive"
    assert states["finding:lib/util.py"] == "fixed"
    moved = next(f for f in store.list() if f.path == "tests/test_x.py")
    assert moved.line == 7 and moved.occurrences == 2 and moved.first_seen == 100.0 and moved.last_seen == 200.0

    # Third run: the critical one is back → reopened.
    third = fs.parse_findings_text("- lib/util.py:12 Something critical happens here\n", source="scan", run_id="r3")
    assert store.record(third, run_id="r3", now=300.0) == (0, 0, 1)
    assert store.get(third[0].fingerprint).state == "open"  # type: ignore[union-attr]
    assert store.gate(max_severity="critical").passed is False
    assert store.gate(max_severity="critical", states=("triaged",)).passed

    table = store.render()
    assert "FINGERPRINT" in table and "lib/util.py:12" in table
    store.close()


def test_sarif_shape(tmp_path: Path) -> None:
    store = fs.FindingsStore()
    store.record(fs.parse_findings_text(SCAN, source="scan"), run_id="r1")
    sarif = store.to_sarif(tool_name="bog-scan", tool_version="1.0")
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "bog-scan" and run["tool"]["driver"]["version"] == "1.0"
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert rule_ids == {"SQLI", "finding"}
    result = next(r for r in run["results"] if r["ruleId"] == "SQLI")
    assert result["level"] == "error" and result["locations"][0]["physicalLocation"]["region"]["startLine"] == 42
    assert result["partialFingerprints"]["bogFingerprint/v1"].startswith("sha256:")
    path = fs.dump_sarif(store, tmp_path / "out" / "findings.sarif", tool_name="bog-scan")
    assert json.loads(path.read_text(encoding="utf-8"))["runs"][0]["results"]
    store.close()
