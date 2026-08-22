"""Full-text search over session history (Tier-1 #4).

Grok Build keeps session history as append-only JSONL (the source of truth) and
a *rebuildable* SQLite FTS index over titles/prompts so `grok sessions search`
is instant. bog's source of truth is the LangGraph SQLite checkpointer
(`~/.bog-agents/sessions.db`); this module adds the sibling **rebuildable FTS
index** (`~/.bog-agents/sessions_fts.db`) plus a search API and a populate
helper, so `/threads search <query>` can find an old thread by what was said in
it — not just by recency.

The index is a cache: it can be dropped and rebuilt from the checkpointer at any
time without losing history. It is maintained *incrementally* — each search
reconciles only the threads whose title changed since last time (in one WAL
transaction, on a worker thread) rather than rebuilding from scratch — so search
stays instant even at power-user thread counts (PERF-1). FTS5 is used when the
runtime's sqlite3 supports it (the common case) and the code degrades to a
`LIKE` scan otherwise, so search always works.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

# Shared with the SDK's memory index so both FTS surfaces tokenize identically.
from bog_agents.hybrid_memory import fts_match_expression, query_terms


@dataclass
class SessionHit:
    """One search result.

    Attributes:
        thread_id: The matching thread's id.
        title: The thread's title/summary (may be empty).
        snippet: A short excerpt around the match (best-effort).
        score: Relevance score (lower is better with BM25; 0.0 for LIKE).
    """

    thread_id: str
    title: str
    snippet: str
    score: float


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True if this sqlite3 build supports FTS5."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True


class SessionSearchIndex:
    """A rebuildable full-text index over session threads.

    Backed by SQLite FTS5 when available, else a `LIKE` fallback. Safe to drop
    and rebuild from the checkpointer at any time.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Open (creating if needed) the index at ``db_path``."""
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        # This is a rebuildable cache, never the source of truth, so trade
        # durability for speed: WAL keeps readers off the writer's back and
        # synchronous=OFF drops the per-commit fsync that made a full rebuild
        # cost seconds (PERF-1). A crash at worst loses cache rows we can
        # re-derive from the checkpointer.
        with suppress(sqlite3.OperationalError):
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=OFF")
        self._fts = _fts5_available(self._conn)
        self._create_schema()

    def _create_schema(self) -> None:
        if self._fts:
            # `thread_id` is UNINDEXED (stored, not tokenized); title/body are searchable.
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts "
                "USING fts5(thread_id UNINDEXED, title, body, tokenize='porter unicode61')"
            )
        else:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "thread_id TEXT PRIMARY KEY, title TEXT, body TEXT)"
            )
        self._conn.commit()

    def index(self, thread_id: str, title: str, body: str) -> None:
        """Insert or replace the searchable text for one thread."""
        if not thread_id:
            return
        if self._fts:
            self._conn.execute(
                "DELETE FROM sessions_fts WHERE thread_id = ?", (thread_id,)
            )
            self._conn.execute(
                "INSERT INTO sessions_fts (thread_id, title, body) VALUES (?, ?, ?)",
                (thread_id, title or "", body or ""),
            )
        else:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (thread_id, title, body) VALUES (?, ?, ?)",
                (thread_id, title or "", body or ""),
            )
        self._conn.commit()

    def remove(self, thread_id: str) -> None:
        """Drop a thread from the index."""
        table = "sessions_fts" if self._fts else "sessions"
        self._conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))  # noqa: S608 - table name is a fixed literal
        self._conn.commit()

    def clear(self) -> None:
        """Empty the index (before a full rebuild)."""
        table = "sessions_fts" if self._fts else "sessions"
        self._conn.execute(f"DELETE FROM {table}")  # noqa: S608 - table name is a fixed literal
        self._conn.commit()

    def reconcile(self, threads: list[tuple[str, str]]) -> None:
        """Incrementally sync the index to `threads` in a single transaction.

        Only the difference is written: threads whose title is unchanged are
        left untouched, changed/new titles are re-inserted, and threads no
        longer present are dropped. The whole diff commits once, so a rebuild
        that used to issue one fsync-backed transaction per thread (seconds at
        power-user thread counts) is now a handful of statements (PERF-1).

        Args:
            threads: `(thread_id, title)` pairs to index; `title` doubles as
                the searchable body today (message-body indexing is a follow-up).
        """
        table = "sessions_fts" if self._fts else "sessions"
        desired: dict[str, str] = {}
        for thread_id, title in threads:
            if thread_id:
                desired[thread_id] = title or ""

        existing: dict[str, str] = {
            str(row[0]): str(row[1] or "")
            for row in self._conn.execute(f"SELECT thread_id, title FROM {table}")  # noqa: S608 - fixed literal
        }

        to_delete = [
            tid
            for tid in existing
            if tid not in desired or existing[tid] != desired[tid]
        ]
        to_insert = [
            (tid, title) for tid, title in desired.items() if existing.get(tid) != title
        ]

        if not to_delete and not to_insert:
            return

        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN")
            if to_delete:
                cur.executemany(
                    f"DELETE FROM {table} WHERE thread_id = ?",  # noqa: S608 - fixed literal
                    [(tid,) for tid in to_delete],
                )
            if to_insert:
                if self._fts:
                    cur.executemany(
                        "INSERT INTO sessions_fts (thread_id, title, body) VALUES (?, ?, ?)",
                        [(tid, title, title) for tid, title in to_insert],
                    )
                else:
                    cur.executemany(
                        "INSERT OR REPLACE INTO sessions (thread_id, title, body) VALUES (?, ?, ?)",
                        [(tid, title, title) for tid, title in to_insert],
                    )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def count(self) -> int:
        """Number of indexed threads."""
        table = "sessions_fts" if self._fts else "sessions"
        row = self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 - fixed literal
        return int(row[0]) if row else 0

    def search(self, query: str, *, limit: int = 20) -> list[SessionHit]:
        """Return threads matching ``query``, most-relevant first.

        Args:
            query: Free-text query. With FTS5 this is an FTS MATCH expression
                (a bare word or phrase works); with the LIKE fallback it is a
                substring.
            limit: Maximum results.

        Returns:
            A list of `SessionHit` (empty when nothing matches or the query is
            blank).
        """
        query = (query or "").strip()
        if not query:
            return []
        if self._fts:
            return self._search_fts(query, limit)
        return self._search_like(query, limit)

    def _search_fts(self, query: str, limit: int) -> list[SessionHit]:
        # Each term is quoted (so punctuation/operators in a user's text can't
        # produce a syntax error) and OR-ed, so BM25 ranks by how many terms a
        # thread matches. Quoting the whole query as one phrase instead made
        # any multi-word search return nothing.
        match = fts_match_expression(query)
        try:
            rows = self._conn.execute(
                "SELECT thread_id, title, "
                "snippet(sessions_fts, 2, '[', ']', '…', 12) AS snip, "
                "bm25(sessions_fts) AS score "
                "FROM sessions_fts WHERE sessions_fts MATCH ? ORDER BY score LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            SessionHit(
                thread_id=r[0], title=r[1] or "", snippet=r[2] or "", score=float(r[3])
            )
            for r in rows
        ]

    def _search_like(self, query: str, limit: int) -> list[SessionHit]:
        # Match any term, mirroring the FTS path so the fallback does not
        # silently return fewer results for a multi-word query.
        terms = query_terms(query) or [query]
        clause = " OR ".join(["title LIKE ? OR body LIKE ?"] * len(terms))
        params: list[object] = []
        for term in terms:
            params.extend([f"%{term}%", f"%{term}%"])
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT DISTINCT thread_id, title, body FROM sessions WHERE {clause} LIMIT ?",  # noqa: S608 - clause is built from a fixed literal repeated per term; all values are bound
            params,
        ).fetchall()
        hits: list[SessionHit] = []
        for thread_id, title, body in rows:
            snippet = _excerpt(body or "", query)
            hits.append(
                SessionHit(
                    thread_id=thread_id, title=title or "", snippet=snippet, score=0.0
                )
            )
        return hits

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> Self:
        """Return the open index for use as a context manager."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the index on context exit."""
        self.close()


def _excerpt(body: str, query: str, *, width: int = 60) -> str:
    """A best-effort excerpt around the first case-insensitive match of query."""
    idx = body.lower().find(query.lower())
    if idx < 0:
        return body[:width]
    start = max(0, idx - width // 2)
    end = min(len(body), idx + len(query) + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{body[start:end]}{suffix}"


def default_index_path() -> Path:
    """Path to the FTS index, a sibling of the checkpointer's ``sessions.db``."""
    from bog_agents_cli.sessions import get_db_path

    return get_db_path().parent / "sessions_fts.db"


def _reconcile_and_search(
    pairs: list[tuple[str, str]], query: str, limit: int
) -> list[SessionHit]:
    """Open the index, incrementally sync it to `pairs`, and search (sync).

    Runs entirely on a worker thread (see `search_sessions`) so the sqlite work
    never blocks the event loop.
    """
    index = SessionSearchIndex(default_index_path())
    try:
        index.reconcile(pairs)
        return index.search(query, limit=limit)
    finally:
        index.close()


async def search_sessions(query: str, *, limit: int = 20) -> list[SessionHit]:
    """Search past threads for ``query`` and return the best matches.

    Incrementally reconciles the FTS index against the checkpointer's thread
    list (title/summary text) and runs the search on a worker thread, so a
    large index never freezes the UI. Message-body indexing is a follow-up;
    today the searchable text is each thread's title/summary. Best-effort — a
    store error yields no results rather than raising.

    Args:
        query: Free-text query.
        limit: Maximum results.

    Returns:
        Ranked `SessionHit`s (empty on a blank query or store error).
    """
    if not query.strip():
        return []
    try:
        from bog_agents_cli.sessions import list_threads

        threads = await list_threads(limit=1000)
    except Exception:  # search must never crash on a store hiccup
        threads = []
    pairs = [
        (
            str(thread.get("thread_id") or ""),
            str(thread.get("summary") or thread.get("agent_name") or ""),
        )
        for thread in threads
    ]
    # The whole index sync + query runs off the Textual event loop (PERF-1).
    return await asyncio.to_thread(_reconcile_and_search, pairs, query, limit)


def format_search_results(query: str, hits: list[SessionHit]) -> str:
    """Render search hits as a Rich-markup message body.

    Every interpolated value is escaped. Titles and snippets are arbitrary
    session text, and the FTS snippet wraps each match in `[`...`]`, so
    unescaped they are parsed as markup: a match naming a real style
    (`[bold]`) is swallowed, and any other (`[deploy]`) fails the parse and
    drops the whole message to literal, exposing the raw tags.

    Args:
        query: The user's search text.
        hits: Matching threads, best first.

    Returns:
        The message body to mount.
    """
    from rich.markup import escape

    if not hits:
        return f"No threads matched '{escape(query)}'."
    lines = [f"[bold]Threads matching '{escape(query)}':[/bold]"]
    for hit in hits:
        title = escape(hit.title or "(untitled)")
        snippet = f" — {escape(hit.snippet)}" if hit.snippet else ""
        lines.append(f"  [cyan]{hit.thread_id[:12]}[/cyan]  {title}{snippet}")
    lines.append("\nResume one with [bold]/resume <thread-id>[/bold].")
    return "\n".join(lines)


__all__ = [
    "SessionHit",
    "SessionSearchIndex",
    "default_index_path",
    "format_search_results",
    "search_sessions",
]
