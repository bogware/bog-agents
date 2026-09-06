"""Org usage export: daily aggregates per user / model / job as CSV and OTLP metrics (ROADMAP #74).

The daemon already prices every run into its `SpendLedger` (`spend.db`, scope
`daemon:<job_id>`), and the CLI keeps the interactive `~/.bog-agents/spend.db`
with `user` / `project:<key>` scopes. This module reads such a ledger, rolls
the rows up per `(day, scope, model)` and writes them as CSV (`usage.csv`) or
posts them as OTLP/HTTP JSON metrics (`bog.usage.usd`, `bog.usage.input_tokens`,
`bog.usage.output_tokens`, `bog.usage.records`) so a platform team can chart
spend per user, job and model without scraping logs. Totals are provably the
ledger's own: the tests check the CSV sums equal `SpendLedger.totals_by_scope`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.spend_ledger import SpendLedger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

CSV_COLUMNS = ("day", "scope", "kind", "owner", "model", "records", "input_tokens", "output_tokens", "usd")


@dataclass(frozen=True)
class UsageRow:
    """One daily aggregate."""

    day: str
    scope: str
    model: str
    records: int
    input_tokens: int
    output_tokens: int
    usd: float

    @property
    def kind(self) -> str:
        """`user` / `project` / `daemon` / other — the scope's prefix."""
        return self.scope.split(":", 1)[0] if ":" in self.scope else self.scope

    @property
    def owner(self) -> str:
        """The scope's identity part (project key, job id, …) or the scope itself."""
        return self.scope.split(":", 1)[1] if ":" in self.scope else self.scope

    def as_csv_row(self) -> list[str]:
        """Values in `CSV_COLUMNS` order."""
        return [
            self.day,
            self.scope,
            self.kind,
            self.owner,
            self.model,
            str(self.records),
            str(self.input_tokens),
            str(self.output_tokens),
            f"{self.usd:.6f}",
        ]


def aggregate_usage(db_path: str | Path, *, since_days: float | None = None, now: float | None = None) -> list[UsageRow]:
    """Roll the ledger up per `(day, scope, model)`, oldest day first."""
    ledger = SpendLedger(db_path)
    try:
        rows = ledger.records(since=(time.time() if now is None else now) - since_days * 86400.0 if since_days is not None else None)
    finally:
        ledger.close()
    totals: dict[tuple[str, str, str], list[float]] = {}
    for record in rows:
        key = (record["day"], record["scope"], record["model"])
        bucket = totals.setdefault(key, [0, 0, 0, 0.0])
        bucket[0] += 1
        bucket[1] += int(record["input_tokens"])
        bucket[2] += int(record["output_tokens"])
        bucket[3] += float(record["usd"])
    return [
        UsageRow(day=day, scope=scope, model=model, records=int(b[0]), input_tokens=int(b[1]), output_tokens=int(b[2]), usd=float(b[3]))
        for (day, scope, model), b in sorted(totals.items())
    ]


def to_csv(rows: Iterable[UsageRow]) -> str:
    """CSV text with a header."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow(row.as_csv_row())
    return buffer.getvalue()


def _attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def to_otlp_metrics(rows: Iterable[UsageRow], *, service_name: str = "bog-agents-daemon", now_ns: int | None = None) -> dict[str, Any]:
    """The `POST /v1/metrics` body: one sum per measure, a data point per aggregate row."""
    stamp = time.time_ns() if now_ns is None else now_ns
    rows = list(rows)

    def points(pick: Callable[[UsageRow], float | int], *, as_int: bool) -> list[dict[str, Any]]:
        out = []
        for row in rows:
            attributes = [
                _attr("bog.day", row.day),
                _attr("bog.scope", row.scope),
                _attr("bog.kind", row.kind),
                _attr("bog.owner", row.owner),
                _attr("gen_ai.request.model", row.model),
            ]
            value = pick(row)
            point: dict[str, Any] = {"timeUnixNano": str(stamp), "attributes": attributes}
            if as_int:
                point["asInt"] = str(int(value))
            else:
                point["asDouble"] = float(value)
            out.append(point)
        return out

    def metric(name: str, unit: str, pick: Callable[[UsageRow], float | int], *, as_int: bool) -> dict[str, Any]:
        return {"name": name, "unit": unit, "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": points(pick, as_int=as_int)}}

    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": [_attr("service.name", service_name)]},
                "scopeMetrics": [
                    {
                        "scope": {"name": "bog_agents_daemon.usage_export"},
                        "metrics": [
                            metric("bog.usage.usd", "USD", lambda r: r.usd, as_int=False),
                            metric("bog.usage.input_tokens", "{token}", lambda r: r.input_tokens, as_int=True),
                            metric("bog.usage.output_tokens", "{token}", lambda r: r.output_tokens, as_int=True),
                            metric("bog.usage.records", "{record}", lambda r: r.records, as_int=True),
                        ],
                    }
                ],
            }
        ]
    }


def post_otlp_metrics(endpoint: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> None:
    """POST the metrics body to a collector (`/v1/metrics` is appended to a base URL)."""
    url = endpoint if endpoint.rstrip("/").endswith("/v1/metrics") else endpoint.rstrip("/") + "/v1/metrics"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", **(headers or {})}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def export_usage(
    db_path: str | Path,
    *,
    since_days: float | None = None,
    csv_path: str | Path | None = None,
    otlp_endpoint: str | None = None,
    otlp_headers: dict[str, str] | None = None,
    post: Callable[..., None] | None = None,
) -> tuple[list[UsageRow], list[str]]:
    """Aggregate and deliver; returns `(rows, notes)` where notes say what was written / posted."""
    rows = aggregate_usage(db_path, since_days=since_days)
    notes: list[str] = []
    if csv_path is not None:
        target = Path(csv_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(to_csv(rows), encoding="utf-8")
        notes.append(f"wrote {len(rows)} row(s) to {target}")
    if otlp_endpoint:
        try:
            (post or post_otlp_metrics)(otlp_endpoint, to_otlp_metrics(rows), headers=otlp_headers)
            notes.append(f"posted {len(rows)} row(s) to {otlp_endpoint}")
        except Exception as exc:
            notes.append(f"OTLP post to {otlp_endpoint} failed: {exc}")
            logger.warning("usage export: OTLP post failed", exc_info=True)
    return rows, notes


__all__ = ["CSV_COLUMNS", "UsageRow", "aggregate_usage", "export_usage", "post_otlp_metrics", "to_csv", "to_otlp_metrics"]
