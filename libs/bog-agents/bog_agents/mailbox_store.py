"""SQLite-backed `Mailbox` so any process can enqueue for a session (ROADMAP #56).

`teams.Mailbox` is an in-memory inbox for teammates inside one process.
`MailboxStore` keeps the same API (`send`, `inbox`, `drain`, `@all`) on a
SQLite file, so `bog-agents queue --session <name>` in one terminal can drop a
prompt that the TUI in another terminal drains on its next idle tick, and the
TUI can post the outcome back to the sender for `--wait`. WAL mode and
`BEGIN IMMEDIATE` make `drain` exactly-once across processes; every message
is read at most once per member.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents.teams import Attachment, Message

if TYPE_CHECKING:
    from collections.abc import Iterator

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS messages ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, recipient TEXT NOT NULL,"
    " body TEXT NOT NULL, ts REAL NOT NULL, attachments TEXT NOT NULL DEFAULT '[]')",
    "CREATE TABLE IF NOT EXISTS reads (message_id INTEGER NOT NULL, member TEXT NOT NULL, PRIMARY KEY (message_id, member))",
    "CREATE INDEX IF NOT EXISTS messages_recipient ON messages (recipient, id)",
)


_ATTACHMENTS_COL = 5  # index of the attachments JSON column in `_visible_sql`


def _rows_to_messages(rows: list[tuple[object, ...]]) -> list[Message]:
    """Rows from `_visible_sql` (with the attachments JSON column) → `Message`s."""
    out: list[Message] = []
    for r in rows:
        raw = r[_ATTACHMENTS_COL] if len(r) > _ATTACHMENTS_COL else "[]"
        try:
            items = json.loads(str(raw)) if raw else []
        except ValueError:
            items = []
        attachments = tuple(Attachment.from_dict(item) for item in items if isinstance(item, dict))
        out.append(Message(sender=str(r[1]), recipient=str(r[2]), body=str(r[3]), ts=float(r[4]), attachments=attachments))  # type: ignore[arg-type]
    return out


class MailboxStore:
    """Cross-process `Mailbox` on a SQLite file (same API as `teams.Mailbox`)."""

    ALL = "@all"

    def __init__(self, path: str | Path, *, timeout: float = 10.0) -> None:
        """Open (and create) the store at `path`."""
        self.path = Path(path)
        self._timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA:
                conn.execute(statement)
            with contextlib.suppress(sqlite3.OperationalError):  # ROADMAP #76: stores created before attachments existed
                conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    def send(self, sender: str, recipient: str, body: str, *, attachments: tuple[Attachment, ...] = ()) -> Message:
        """Post a message (optionally carrying attachments); returns the stored `Message`."""
        msg = Message(sender=sender, recipient=recipient, body=body, attachments=tuple(attachments))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (sender, recipient, body, ts, attachments) VALUES (?, ?, ?, ?, ?)",
                (msg.sender, msg.recipient, msg.body, msg.ts, json.dumps([a.to_dict() for a in msg.attachments])),
            )
        return msg

    @staticmethod
    def _visible_sql(unread_for: str | None) -> tuple[str, tuple[str, ...]]:
        sql = "SELECT id, sender, recipient, body, ts, attachments FROM messages WHERE recipient IN (?, ?) AND sender != ?"
        if unread_for is not None:
            sql += " AND id NOT IN (SELECT message_id FROM reads WHERE member = ?)"
        sql += " ORDER BY id"
        return sql, ()

    def inbox(self, member: str) -> list[Message]:
        """All messages `member` can see (peek, non-consuming)."""
        sql, _ = self._visible_sql(None)
        with self._connect() as conn:
            rows = conn.execute(sql, (member, self.ALL, member)).fetchall()
        return _rows_to_messages(rows)

    def pending(self, member: str) -> int:
        """How many unread messages wait for `member`."""
        sql, _ = self._visible_sql(member)
        with self._connect() as conn:
            rows = conn.execute(sql, (member, self.ALL, member, member)).fetchall()
        return len(rows)

    def drain(self, member: str) -> list[Message]:
        """Return unread messages for `member` and mark them read (atomic across processes)."""
        sql, _ = self._visible_sql(member)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(sql, (member, self.ALL, member, member)).fetchall()
                if rows:
                    conn.executemany("INSERT OR IGNORE INTO reads (message_id, member) VALUES (?, ?)", [(r[0], member) for r in rows])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return _rows_to_messages(rows)

    def wait(self, member: str, *, timeout: float, poll: float = 0.5) -> list[Message]:
        """Block until `member` has unread messages (drained) or `timeout` seconds pass."""
        deadline = time.monotonic() + timeout
        while True:
            got = self.drain(member)
            if got or time.monotonic() >= deadline:
                return got
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))

    def purge(self, *, older_than: float) -> int:
        """Delete messages older than `older_than` seconds; returns the count removed."""
        cutoff = time.time() - older_than
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [r[0] for r in conn.execute("SELECT id FROM messages WHERE ts < ?", (cutoff,)).fetchall()]
                if ids:
                    marks = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM reads WHERE message_id IN ({marks})", ids)  # noqa: S608 - placeholders only
                    conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)  # noqa: S608 - placeholders only
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(ids)


__all__ = ["MailboxStore", "Message"]
