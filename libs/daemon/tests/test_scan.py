"""ROADMAP #59: scan jobs feed the findings ledger."""

from __future__ import annotations

from pathlib import Path

import pytest
from bog_agents.findings_store import FindingsStore
from fastapi.testclient import TestClient

from bog_agents_daemon import scan
from bog_agents_daemon.models import AmbientJob, JobRun, JobStatus
from bog_agents_daemon.runner import _build_prompt

REPORT = """Scanned 40 files.

## Findings
- src/auth.py:42 [high] SQLI: user input reaches the query
- src/util.py:7 [low] DEAD: unused helper
"""


def test_scan_prompt_profiles_and_custom() -> None:
    job = AmbientJob(name="nightly", scan_profile="security", working_dir="/repo")
    prompt = _build_prompt(job)
    assert "security scan" in prompt and "## Findings" in prompt and "<path>:<line>" in prompt
    custom = AmbientJob(name="c", scan_profile="custom", prompt="No print statements.")
    assert "No print statements." in scan.scan_prompt(custom)
    with pytest.raises(ValueError, match="unknown scan_profile"):
        scan.scan_prompt(AmbientJob(name="x", scan_profile="bogus"))
    with pytest.raises(ValueError, match="no prompt"):
        scan.scan_prompt(AmbientJob(name="x", scan_profile="custom"))


def test_findings_db_path_defaults_under_working_dir(tmp_path: Path) -> None:
    job = AmbientJob(name="n", scan_profile="perf", working_dir=str(tmp_path))
    assert scan.findings_db_path(job) == tmp_path / ".bog-agents" / "findings.db"
    explicit = AmbientJob(name="n", scan_profile="perf", findings_db=str(tmp_path / "x.db"))
    assert scan.findings_db_path(explicit) == tmp_path / "x.db"


def test_record_scan_output_updates_ledger_and_gates(tmp_path: Path) -> None:
    job = AmbientJob(name="nightly", scan_profile="security", working_dir=str(tmp_path), scan_gate="high")
    run = JobRun(job_id=job.job_id, job_name=job.name, output=REPORT, status=JobStatus.COMPLETED)
    summary = scan.record_scan_output(job, run)
    assert (summary.new, summary.updated, summary.reopened, summary.fixed, summary.open_total) == (2, 0, 0, 0, 2)
    assert summary.gate is not None and not summary.gate.passed
    assert "2 new" in summary.describe() and "FAILED" in summary.describe()

    second = JobRun(job_id=job.job_id, job_name=job.name, output="## Findings\n- src/util.py:9 [low] DEAD: unused helper\n")
    summary = scan.record_scan_output(job, second)
    assert (summary.new, summary.updated, summary.fixed, summary.open_total) == (0, 1, 1, 1)
    assert summary.gate is not None and summary.gate.passed

    store = FindingsStore(scan.findings_db_path(job))
    rows = {f.rule_id: f for f in store.list(states=("open", "triaged", "fixed"))}
    assert rows["SQLI"].state == "fixed" and rows["DEAD"].line == 9 and rows["DEAD"].source == "scan:nightly"
    store.close()


def test_run_job_records_scan_and_marks_gate(tmp_path: Path, tmp_daemon_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from bog_agents_daemon import runner

    async def _fake_invoke(job: AmbientJob, prompt: str, **_kwargs: object) -> tuple[str, int, None]:
        assert "## Findings" in prompt
        return REPORT, 1, None

    monkeypatch.setattr(runner, "_invoke_agent_with_retry", _fake_invoke)
    job = AmbientJob(name="nightly", scan_profile="security", working_dir=str(tmp_path), scan_gate="critical")
    run = asyncio.run(runner.run_job(job))
    assert run.status == JobStatus.COMPLETED and "Findings ledger: 2 new" in run.output
    assert run.error == ""  # gate at critical passes with a high finding
    job.scan_gate = "high"
    run2 = asyncio.run(runner.run_job(job))
    assert run2.status == JobStatus.COMPLETED and "findings gate FAILED" in run2.error


def test_findings_api(tmp_path: Path, tmp_daemon_dir: Path) -> None:
    from unittest.mock import AsyncMock

    from bog_agents_daemon.api import create_app
    from bog_agents_daemon.scheduler import DaemonScheduler
    from bog_agents_daemon.store import load_jobs, upsert_job

    job = AmbientJob(name="nightly", scan_profile="security", working_dir=str(tmp_path))
    upsert_job(job)
    scan.record_scan_output(job, JobRun(job_id=job.job_id, job_name=job.name, output=REPORT))
    client = TestClient(create_app(token="t", scheduler=DaemonScheduler(store_loader=load_jobs, runner=AsyncMock())), raise_server_exceptions=True)
    auth = {"X-Daemon-Token": "t"}
    assert client.get("/findings").status_code in (401, 403)
    rows = client.get(f"/findings?job_id={job.job_id}", headers=auth).json()
    assert [r["rule_id"] for r in rows] == ["SQLI", "DEAD"]
    assert client.get("/findings?job_id=nope", headers=auth).status_code == 404
    gate = client.get(f"/findings/gate?job_id={job.job_id}&max_severity=high", headers=auth).json()
    assert gate["passed"] is False and gate["blocking"] == 1
    fp = rows[0]["fingerprint"]
    triaged = client.post(f"/findings/{fp}/triage", json={"job_id": job.job_id, "state": "false_positive", "note": "parameterised"}, headers=auth)
    assert triaged.status_code == 200 and triaged.json()["state"] == "false_positive"
    assert client.post("/findings/sha256:nope/triage", json={"job_id": job.job_id, "state": "fixed"}, headers=auth).status_code == 404
    assert client.post(f"/findings/{fp}/triage", json={"job_id": job.job_id, "state": "bogus"}, headers=auth).status_code == 400
    assert client.get(f"/findings/gate?job_id={job.job_id}&max_severity=high", headers=auth).json()["passed"] is True
    sarif = client.get(f"/findings/sarif?job_id={job.job_id}", headers=auth).json()
    assert sarif["version"] == "2.1.0" and [r["ruleId"] for r in sarif["runs"][0]["results"]] == ["DEAD"]
