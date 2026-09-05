"""Durable spend ledger (ROADMAP #51): dollars per day, per scope.

`CostLedger` answers "what has *this session* spent"; this module answers
"what has this user / this project / this daemon job spent *today*", which is
what a daily ceiling needs. One SQLite table, no dependencies, safe to share
between the CLI and the daemon. Scopes are plain strings — `user`,
`project:<key>`, `daemon:<job_id>` — built by the helpers below so both
packages spell them the same way. `now` is injectable everywhere for tests.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

SCOPE_USER = "user"
"""Scope for everything the current OS user spends, across projects."""


def project_scope(key: str) -> str:
    """Scope for one project (`key` is the caller's stable project id)."""
    return f"project:{key}"


def daemon_scope(job_id: str) -> str:
    """Scope for one daemon job."""
    return f"daemon:{job_id}"


CeilingState = Literal["ok", "warn", "reached"]


@dataclass(frozen=True)
class CeilingStatus:
    """Where today's spend sits against a ceiling.

    Attributes:
        state: `ok`, `warn` (past `warn_at_percent`), or `reached`.
        spent_usd: Today's spend for the scope.
        ceiling_usd: The ceiling, or `None` when none is configured.
        message: Human text for `warn` / `reached`; empty for `ok`.
    """

    state: CeilingState
    spent_usd: float
    ceiling_usd: float | None
    message: str = ""


def check_ceiling(spent_usd: float, ceiling_usd: float | None, *, warn_at_percent: int = 80, label: str = "daily") -> CeilingStatus:
    """Classify `spent_usd` against `ceiling_usd`.

    Args:
        spent_usd: Spend so far in the period.
        ceiling_usd: The ceiling; `None` or non-positive means unlimited.
        warn_at_percent: Percentage of the ceiling at which `warn` starts.
        label: Word used in the rendered message (`daily`, `job`, …).

    Returns:
        The `CeilingStatus`.
    """
    if ceiling_usd is None or ceiling_usd <= 0:
        return CeilingStatus("ok", spent_usd, None)
    if spent_usd >= ceiling_usd:
        return CeilingStatus(
            "reached",
            spent_usd,
            ceiling_usd,
            f"{label} ceiling reached: ${spent_usd:.2f} of ${ceiling_usd:.2f} spent today",
        )
    pct = spent_usd / ceiling_usd * 100
    if pct >= max(0, warn_at_percent):
        return CeilingStatus(
            "warn",
            spent_usd,
            ceiling_usd,
            f"{label} ceiling {pct:.0f}% used: ${spent_usd:.2f} of ${ceiling_usd:.2f}",
        )
    return CeilingStatus("ok", spent_usd, ceiling_usd)


class SpendLedger:
    """Append-only daily spend records in SQLite.

    Args:
        db_path: File path, or `":memory:"` (default) for an ephemeral ledger.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        """Open (creating if needed) the ledger at `db_path`."""
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS spend ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, scope TEXT NOT NULL, "
                "model TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0, "
                "output_tokens INTEGER NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS spend_scope_day ON spend(scope, day)")
            self._conn.commit()

    @staticmethod
    def day_key(ts: float) -> str:
        """Return the local calendar day (`YYYY-MM-DD`) for a Unix timestamp."""
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: DTZ006 - the user's local day is the point

    def record(
        self,
        scope: str,
        usd: float,
        *,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        now: float | None = None,
    ) -> None:
        """Append one spend record.

        Args:
            scope: A scope string (see `SCOPE_USER`, `project_scope`, `daemon_scope`).
            usd: Dollars spent (negative values are clamped to zero).
            model: Model spec, for later breakdowns.
            input_tokens: Input tokens behind the spend.
            output_tokens: Output tokens behind the spend.
            now: Unix timestamp override (tests).
        """
        ts = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "INSERT INTO spend (ts, day, scope, model, input_tokens, output_tokens, usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, self.day_key(ts), scope, model, int(input_tokens), int(output_tokens), max(0.0, float(usd))),
            )
            self._conn.commit()

    def total_usd(self, scope: str, *, now: float | None = None) -> float:
        """Return the scope's spend for the local day containing `now` (default: today)."""
        ts = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE scope = ? AND day = ?", (scope, self.day_key(ts))).fetchone()
        return float(row[0] if row else 0.0)

    def totals_by_scope(self, *, now: float | None = None) -> dict[str, float]:
        """Return `{scope: usd}` for the local day containing `now` (default: today)."""
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute("SELECT scope, SUM(usd) FROM spend WHERE day = ? GROUP BY scope", (self.day_key(ts),)).fetchall()
        return {str(scope): float(total) for scope, total in rows}

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()
