"""ROADMAP #51: the durable daily spend ledger and ceiling checks."""

from __future__ import annotations

from pathlib import Path

from bog_agents.spend_ledger import SCOPE_USER, SpendLedger, check_ceiling, daemon_scope, project_scope

DAY1 = 1_800_000_000.0  # 2027-01-15 local-ish; only the day boundary matters below
DAY2 = DAY1 + 86_400 * 2


def test_records_sum_per_scope_and_day(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.db")
    ledger.record(SCOPE_USER, 0.25, model="m", input_tokens=10, output_tokens=2, now=DAY1)
    ledger.record(SCOPE_USER, 0.50, now=DAY1 + 60)
    ledger.record(project_scope("abc"), 0.10, now=DAY1)
    ledger.record(SCOPE_USER, 9.00, now=DAY2)
    assert ledger.total_usd(SCOPE_USER, now=DAY1) == 0.75
    assert ledger.total_usd(project_scope("abc"), now=DAY1) == 0.10
    assert ledger.total_usd(daemon_scope("j1"), now=DAY1) == 0.0
    assert ledger.total_usd(SCOPE_USER, now=DAY2) == 9.00
    assert ledger.totals_by_scope(now=DAY1) == {SCOPE_USER: 0.75, "project:abc": 0.10}
    ledger.close()
    # Durable: a fresh handle sees the same totals.
    again = SpendLedger(tmp_path / "spend.db")
    assert again.total_usd(SCOPE_USER, now=DAY1) == 0.75


def test_negative_spend_is_clamped() -> None:
    ledger = SpendLedger()
    ledger.record(SCOPE_USER, -3.0, now=DAY1)
    assert ledger.total_usd(SCOPE_USER, now=DAY1) == 0.0


def test_check_ceiling_states() -> None:
    assert check_ceiling(5.0, None).state == "ok"
    assert check_ceiling(5.0, 0).state == "ok"
    assert check_ceiling(1.0, 10.0).state == "ok"
    warn = check_ceiling(8.5, 10.0, warn_at_percent=80)
    assert warn.state == "warn"
    assert "85% used" in warn.message
    reached = check_ceiling(10.0, 10.0, label="job")
    assert reached.state == "reached"
    assert reached.message.startswith("job ceiling reached")
