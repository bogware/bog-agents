"""Cross-agent shared memory tier.

Today's :class:`bog_agents.middleware.MemoryMiddleware` gives each
agent a per-session and per-agent memory store. Sometimes you want
agents to share notes — agent A learns something, agent B picks it
up next time it runs.

The shared store is opt-in and pluggable:

* Default backend: SQLite at ``~/.bog-agents/shared-memory.db``.
* Optional backends (Postgres / Redis / Dynamo) plug in via a thin
  protocol. The CLI only ships the SQLite backend in-tree; everything
  else is a no-op stub until the user installs an adapter package.

Two tools are exposed to the agent when the middleware is enabled:

* ``memory_post_shared(content, tags)`` — write an entry.
* ``memory_search_shared(query, limit=5)`` — substring search the entries.

The middleware also pre-fetches the top-K most-recent matching entries
on every model call and injects them into the system prompt under
``## Shared memory`` so the agent doesn't have to call the tool just
to see relevant context.

Anything that goes wrong (DB locked, disk full, missing module)
degrades to "the tools return an empty result" — never raises into
the prompt path.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from bog_agents_cli.dreamscape.config import (
    SharedMemoryConfig,
    is_emergency_disabled,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SharedMemoryEntry:
    """One row in the shared memory store."""

    id: int
    agent_id: str
    """Who posted this entry."""

    content: str
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at,
        }


class SharedMemoryBackend(Protocol):
    """Minimal protocol an adapter must implement to plug into this middleware."""

    def write(
        self, *, agent_id: str, content: str, tags: list[str]
    ) -> SharedMemoryEntry | None: ...
    def search(self, query: str, *, limit: int = 5) -> list[SharedMemoryEntry]: ...
    def recent(self, *, limit: int = 5) -> list[SharedMemoryEntry]: ...


# ---------------------------------------------------------------------------
# SQLite backend (default)
# ---------------------------------------------------------------------------


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS shared_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    content     TEXT NOT NULL,
    tags_json   TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shared_memory_created
    ON shared_memory (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shared_memory_agent
    ON shared_memory (agent_id);
"""


class SQLiteSharedMemory:
    """The default shared-memory backend. Single SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        with suppress(OSError):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        # WAL keeps reads fast under concurrent writes from other CLIs.
        with suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._conn() as conn:
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            logger.warning(
                "could not initialise shared-memory schema at %s: %s",
                self._path,
                exc,
            )

    def write(
        self, *, agent_id: str, content: str, tags: list[str]
    ) -> SharedMemoryEntry | None:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO shared_memory (agent_id, content, tags_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (agent_id, content, json.dumps(tags), time.time()),
                )
                row_id = cur.lastrowid or 0
                ts_row = conn.execute(
                    "SELECT created_at FROM shared_memory WHERE id=?", (row_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("shared-memory write failed: %s", exc)
            return None
        return SharedMemoryEntry(
            id=row_id,
            agent_id=agent_id,
            content=content,
            tags=tags,
            created_at=ts_row[0] if ts_row else time.time(),
        )

    def search(self, query: str, *, limit: int = 5) -> list[SharedMemoryEntry]:
        like = f"%{query}%"
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, agent_id, content, tags_json, created_at "
                    "FROM shared_memory WHERE content LIKE ? OR tags_json LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (like, like, max(1, min(50, limit))),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("shared-memory search failed: %s", exc)
            return []
        return [_row_to_entry(r) for r in rows]

    def recent(self, *, limit: int = 5) -> list[SharedMemoryEntry]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, agent_id, content, tags_json, created_at "
                    "FROM shared_memory ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(50, limit)),),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("shared-memory recent() failed: %s", exc)
            return []
        return [_row_to_entry(r) for r in rows]


class NoopSharedMemory:
    """Stub backend for configured-but-unavailable backends."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        logger.warning("shared-memory falling back to no-op backend: %s", reason)

    def write(
        self, *, agent_id: str, content: str, tags: list[str]
    ) -> SharedMemoryEntry | None:
        del agent_id, content, tags
        return None

    def search(self, query: str, *, limit: int = 5) -> list[SharedMemoryEntry]:
        del query, limit
        return []

    def recent(self, *, limit: int = 5) -> list[SharedMemoryEntry]:
        del limit
        return []


def _row_to_entry(row: tuple[Any, ...]) -> SharedMemoryEntry:
    try:
        tags = json.loads(row[3]) if row[3] else []
    except (TypeError, ValueError):
        tags = []
    return SharedMemoryEntry(
        id=int(row[0]),
        agent_id=str(row[1]),
        content=str(row[2]),
        tags=list(tags) if isinstance(tags, list) else [],
        created_at=float(row[4]),
    )


def build_backend(cfg: SharedMemoryConfig) -> SharedMemoryBackend:
    """Construct the configured backend; fall back to no-op on failure."""
    backend = (cfg.backend or "sqlite").strip().lower()
    if backend == "sqlite":
        return SQLiteSharedMemory(Path(cfg.sqlite_path).expanduser())
    if backend in {"postgres", "redis", "dynamo"}:
        return NoopSharedMemory(
            f"backend {backend!r} requires an adapter package that's not installed"
        )
    return NoopSharedMemory(f"unknown backend {backend!r}")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_secrets(content: str, patterns: list[str]) -> str:
    """Replace anything matching ``patterns`` with ``[redacted]``.

    Best-effort — we'd rather over-redact (slight loss of info) than
    write a key into a shared store. Each pattern is compiled with
    ``re.IGNORECASE`` and failing patterns are skipped with a log.
    """
    redacted = content
    for pattern in patterns:
        try:
            redacted = re.sub(pattern, "[redacted]", redacted, flags=re.IGNORECASE)
        except re.error as exc:
            logger.warning(
                "shared-memory redact pattern %r is invalid: %s", pattern, exc
            )
    return redacted


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


# No durable LangGraph state — entries live in the SQLite store.


class SharedMemoryMiddleware(AgentMiddleware):
    """Cross-agent memory tier exposed as two tools.

    Inert when ``cfg.enabled=False`` or the emergency disable is set —
    no tools attached, no system-prompt mutation.
    """

    def __init__(
        self,
        *,
        agent_id: str = "default",
        cfg: SharedMemoryConfig | None = None,
        backend: SharedMemoryBackend | None = None,
    ) -> None:
        self._agent_id = agent_id or "default"
        self._cfg = cfg or SharedMemoryConfig()
        self._backend: SharedMemoryBackend = backend or (
            build_backend(self._cfg)
            if self._cfg.enabled
            else NoopSharedMemory("disabled")
        )
        self._tools_cache: list[Any] | None = None

    @property
    def active(self) -> bool:
        return self._cfg.enabled and not is_emergency_disabled()

    @property
    def tools(self) -> list[Any]:
        """Expose post + search as agent tools when active."""
        if not self.active:
            return []
        if self._tools_cache is not None:
            return self._tools_cache
        try:
            self._tools_cache = self._build_tools()
        except Exception:
            logger.exception("SharedMemoryMiddleware: tool construction failed")
            self._tools_cache = []
        return self._tools_cache

    def _build_tools(self) -> list[Any]:
        try:
            from langchain_core.tools import StructuredTool
            from pydantic import BaseModel, Field
        except ImportError:
            logger.debug(
                "langchain_core/pydantic missing; skipping shared-memory tools"
            )
            return []

        class PostArgs(BaseModel):
            content: str = Field(
                ..., description="The note to share with other agents."
            )
            tags: list[str] = Field(
                default_factory=list,
                description="Optional tags for searchability.",
            )

        class SearchArgs(BaseModel):
            query: str = Field(..., description="Substring to search for.")
            limit: int = Field(
                default=5, ge=1, le=20, description="Max entries to return."
            )

        backend = self._backend
        cfg = self._cfg
        agent_id = self._agent_id

        def post(content: str, tags: list[str]) -> str:
            if not isinstance(content, str) or not content.strip():
                return "rejected: content must be a non-empty string"
            if len(content) > cfg.max_entry_chars:
                content = content[: cfg.max_entry_chars] + "…[truncated]"
            redacted = redact_secrets(content, cfg.redact_secret_patterns)
            tag_list = [str(t).strip()[:64] for t in tags if str(t).strip()]
            entry = backend.write(agent_id=agent_id, content=redacted, tags=tag_list)
            if entry is None:
                return "shared-memory write failed (logged; nothing persisted)"
            return f"saved entry #{entry.id}"

        def search(query: str, limit: int = 5) -> str:
            if not isinstance(query, str) or not query.strip():
                return "[]"
            entries = backend.search(query, limit=limit)
            return json.dumps([e.to_dict() for e in entries])

        return [
            StructuredTool.from_function(
                func=post,
                name="memory_post_shared",
                description=(
                    "Post a note to shared memory so OTHER agents can read it "
                    "next time they run. Tags are optional and used for search. "
                    "Secrets matching common patterns are auto-redacted."
                ),
                args_schema=PostArgs,
            ),
            StructuredTool.from_function(
                func=search,
                name="memory_search_shared",
                description=(
                    "Substring-search shared memory. Returns up to N entries "
                    "as JSON. Use to recall what other agents have posted."
                ),
                args_schema=SearchArgs,
            ),
        ]

    # ------------------------------------------------------------------
    # Inject recent entries into the system prompt
    # ------------------------------------------------------------------

    _SHARED_MEMORY_HEADER = "## Shared memory (recent entries)"

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if self.active:
            request = self._maybe_inject(request)
        return call_next(request)

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if self.active:
            request = self._maybe_inject(request)
        return await call_next(request)

    def _maybe_inject(self, request: ModelRequest) -> ModelRequest:
        try:
            entries = self._backend.recent(limit=5)
        except Exception:
            logger.exception("SharedMemoryMiddleware: recent() failed")
            return request
        if not entries:
            return request
        lines = [self._SHARED_MEMORY_HEADER, ""]
        for entry in entries:
            tag_str = f" [{','.join(entry.tags)}]" if entry.tags else ""
            lines.append(f"- ({entry.agent_id}){tag_str}: {entry.content[:240]}")
        try:
            from bog_agents.middleware._utils import append_to_system_message

            return request.override(
                system_message=append_to_system_message(
                    request.system_message, "\n".join(lines)
                )
            )
        except Exception:
            logger.exception("SharedMemoryMiddleware: append_to_system_message failed")
            return request
