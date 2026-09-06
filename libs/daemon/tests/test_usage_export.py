"""ROADMAP #74: usage aggregates equal the ledger and export as CSV / OTLP metrics."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from bog_agents.spend_ledger import SpendLedger, daemon_scope, project_scope

from bog_agents_daemon import usage_export as ux


def _seed(db: Path) -> SpendLedger:
    ledger = SpendLedger(db)
    day1 = 1_800_000_000.0
    ledger.record("user", 0.5, model="anthropic:claude-x", input_tokens=1000, output_tokens=100, now=day1)
    ledger.record("user", 0.25, model="anthropic:claude-x", input_tokens=500, output_tokens=50, now=day1 + 60)
    ledger.record(project_scope("repo"), 0.1, model="ollama:llama", input_tokens=10, output_tokens=5, now=day1 + 120)
    ledger.record(daemon_scope("job-1"), 2.0, model="anthropic:claude-y", input_tokens=4000, output_tokens=400, now=day1 + 86_400 * 3)
    return ledger


def test_aggregates_match_the_ledger(tmp_path: Path) -> None:
    ledger = _seed(tmp_path / "spend.db")
    rows = ux.aggregate_usage(tmp_path / "spend.db")
    assert [(r.scope, r.model, r.records) for r in rows] == [
        ("project:repo", "ollama:llama", 1),
        ("user", "anthropic:claude-x", 2),
        ("daemon:job-1", "anthropic:claude-y", 1),
    ]
    user_row = next(r for r in rows if r.scope == "user")
    assert (user_row.input_tokens, user_row.output_tokens, user_row.usd) == (1500, 150, 0.75)
    assert user_row.kind == "user" and rows[0].kind == "project" and rows[0].owner == "repo"
    by_scope: dict[str, float] = {}
    for row in rows:
        by_scope[row.scope] = by_scope.get(row.scope, 0.0) + row.usd
    day1_totals = ledger.totals_by_scope(now=1_800_000_000.0)
    for scope, total in day1_totals.items():
        assert abs(by_scope[scope] - total) < 1e-9
    ledger.close()
    recent = ux.aggregate_usage(tmp_path / "spend.db", since_days=1, now=1_800_000_000.0 + 86_400 * 3 + 10)
    assert [r.scope for r in recent] == ["daemon:job-1"]


def test_csv_and_otlp_metrics(tmp_path: Path) -> None:
    _seed(tmp_path / "spend.db").close()
    rows = ux.aggregate_usage(tmp_path / "spend.db")
    text = ux.to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0].keys() == set(ux.CSV_COLUMNS)
    assert sum(float(r["usd"]) for r in parsed) == sum(r.usd for r in rows)

    metrics = ux.to_otlp_metrics(rows, now_ns=5)
    names = [m["name"] for m in metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]]
    assert names == ["bog.usage.usd", "bog.usage.input_tokens", "bog.usage.output_tokens", "bog.usage.records"]
    usd_points = metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["dataPoints"]
    assert len(usd_points) == len(rows) and usd_points[0]["timeUnixNano"] == "5"
    attrs = {a["key"]: a["value"]["stringValue"] for a in usd_points[0]["attributes"]}
    assert attrs["bog.scope"] == "project:repo" and attrs["gen_ai.request.model"] == "ollama:llama"

    posted: list[tuple[str, dict]] = []
    out_rows, notes = ux.export_usage(
        tmp_path / "spend.db",
        csv_path=tmp_path / "out" / "usage.csv",
        otlp_endpoint="http://collector:4318",
        post=lambda endpoint, payload, headers=None: posted.append((endpoint, payload)),
    )
    assert len(out_rows) == 3 and (tmp_path / "out" / "usage.csv").read_text(encoding="utf-8").startswith("day,scope")
    assert posted and posted[0][0] == "http://collector:4318"
    assert any("wrote 3 row(s)" in n for n in notes) and any("posted 3 row(s)" in n for n in notes)

    def failing(endpoint: str, payload: dict, headers: dict | None = None) -> None:
        raise OSError("down")

    _rows, notes = ux.export_usage(tmp_path / "spend.db", otlp_endpoint="http://x", post=failing)
    assert any("failed" in n for n in notes)
