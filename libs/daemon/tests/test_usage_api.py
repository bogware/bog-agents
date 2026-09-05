"""ROADMAP #74: `GET /usage` and `POST /usage/export`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bog_agents.spend_ledger import SpendLedger, daemon_scope
from fastapi.testclient import TestClient

from bog_agents_daemon import usage_export
from bog_agents_daemon.api import create_app
from bog_agents_daemon.scheduler import DaemonScheduler
from bog_agents_daemon.store import load_jobs, spend_db_path

_TOKEN = "usage-token"


@pytest.fixture()
def client(tmp_daemon_dir: Path) -> TestClient:
    ledger = SpendLedger(spend_db_path())
    ledger.record(daemon_scope("job-a"), 1.5, model="anthropic:claude-x", input_tokens=1000, output_tokens=100)
    ledger.record(daemon_scope("job-a"), 0.5, model="anthropic:claude-x", input_tokens=200, output_tokens=20)
    ledger.record(daemon_scope("job-b"), 0.25, model="ollama:llama", input_tokens=10, output_tokens=1)
    ledger.close()
    scheduler = DaemonScheduler(store_loader=load_jobs, runner=AsyncMock())
    return TestClient(create_app(token=_TOKEN, scheduler=scheduler), raise_server_exceptions=True)


def test_usage_rows(client: TestClient) -> None:
    auth = {"X-Daemon-Token": _TOKEN}
    assert client.get("/usage").status_code in (401, 403)
    rows = client.get("/usage?days=7", headers=auth).json()
    assert {(r["scope"], r["model"], r["records"]) for r in rows} == {("daemon:job-a", "anthropic:claude-x", 2), ("daemon:job-b", "ollama:llama", 1)}
    job_a = next(r for r in rows if r["owner"] == "job-a")
    assert job_a["kind"] == "daemon" and job_a["usd"] == 2.0 and job_a["input_tokens"] == 1200


def test_usage_export_writes_csv_and_posts(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth = {"X-Daemon-Token": _TOKEN}
    posted: list[str] = []
    monkeypatch.setattr(usage_export, "post_otlp_metrics", lambda endpoint, payload, headers=None, timeout=10.0: posted.append(endpoint))
    target = tmp_path / "out" / "usage.csv"
    resp = client.post("/usage/export", headers=auth, json={"days": 7, "csv_path": str(target), "otlp_endpoint": "http://collector:4318"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["rows"] == 2 and any("wrote 2 row(s)" in n for n in body["notes"]) and any("posted 2 row(s)" in n for n in body["notes"])
    assert target.read_text(encoding="utf-8").startswith("day,scope") and posted == ["http://collector:4318"]
    assert client.post("/usage/export", headers=auth, json={"days": 0}).status_code == 422
