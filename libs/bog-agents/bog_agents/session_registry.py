"""Per-machine session registry (ROADMAP #56): `~/.bog-agents/sessions/<id>.json`.

Every long-lived agent host — the TUI, the daemon per run, `serve` — writes one
small JSON record (name, kind, cwd, model, state, pid, heartbeat, thread id,
server URL, mailbox path) and refreshes its heartbeat while it lives, so
`bog-agents sessions` can list what runs on this machine, `bog-agents queue`
can address a session by name and `bog-agents attach` can find a detached
session's server. The store is a directory of files; liveness is "heartbeat
fresh, or pid alive"; every writer is best effort and never raises into the
host that called it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STATES = ("starting", "idle", "busy", "detached", "draining", "exited")
DEFAULT_STALE_AFTER = 90.0
"""Seconds without a heartbeat after which a record with no live pid is stale."""
_STILL_ACTIVE = 259  # GetExitCodeProcess for a running process
_AGE_UNITS = ((90.0, 1.0, "s"), (5400.0, 60.0, "m"), (172800.0, 3600.0, "h"))


def default_registry_dir() -> Path:
    """`~/.bog-agents/sessions`, or `$BOG_AGENTS_SESSIONS_DIR` (tests, containers)."""
    override = os.environ.get("BOG_AGENTS_SESSIONS_DIR")
    return Path(override) if override else Path.home() / ".bog-agents" / "sessions"


@dataclass
class SessionRecord:
    """One running (or detached) agent host on this machine."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    kind: str = "tui"  # tui | daemon | serve
    cwd: str = ""
    model: str = ""
    state: str = "starting"
    pid: int = field(default_factory=os.getpid)
    heartbeat: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    thread_id: str = ""
    server_url: str = ""
    server_pid: int = 0
    mailbox_path: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionRecord:
        """Build from a stored mapping, ignoring unknown keys."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]

    @property
    def label(self) -> str:
        """Name when set, else the id."""
        return self.name or self.session_id


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists (never signals it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _path(session_id: str, registry_dir: Path | None) -> Path:
    return (registry_dir or default_registry_dir()) / f"{session_id}.json"


def _write(record: SessionRecord, registry_dir: Path | None) -> None:
    path = _path(record.session_id, registry_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


def register(record: SessionRecord, *, registry_dir: Path | None = None) -> SessionRecord:
    """Write the record (best effort) and return it."""
    try:
        _write(record, registry_dir)
    except OSError:
        logger.debug("Could not write session record %s", record.session_id, exc_info=True)
    return record


def load_session(session_id: str, *, registry_dir: Path | None = None) -> SessionRecord | None:
    """The stored record, or `None` when missing or unreadable."""
    path = _path(session_id, registry_dir)
    try:
        return SessionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def heartbeat(
    session_id: str,
    *,
    state: str | None = None,
    thread_id: str | None = None,
    model: str | None = None,
    server_url: str | None = None,
    server_pid: int | None = None,
    detail: str | None = None,
    registry_dir: Path | None = None,
) -> SessionRecord | None:
    """Refresh the heartbeat and any changed fields; `None` when the record is gone."""
    record = load_session(session_id, registry_dir=registry_dir)
    if record is None:
        return None
    record.heartbeat = time.time()
    if state is not None:
        record.state = state
    if thread_id is not None:
        record.thread_id = thread_id
    if model is not None:
        record.model = model
    if server_url is not None:
        record.server_url = server_url
    if server_pid is not None:
        record.server_pid = server_pid
    if detail is not None:
        record.detail = detail
    return register(record, registry_dir=registry_dir)


def unregister(session_id: str, *, registry_dir: Path | None = None) -> bool:
    """Remove the record; `True` when one was removed."""
    path = _path(session_id, registry_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("Could not remove session record %s", session_id, exc_info=True)
        return False
    return True


def is_live(record: SessionRecord, *, stale_after: float = DEFAULT_STALE_AFTER, now: float | None = None) -> bool:
    """Fresh heartbeat, or a pid that still exists (a detached server keeps its record alive)."""
    if record.state == "exited":
        return False
    age = (now if now is not None else time.time()) - record.heartbeat
    if age <= stale_after:
        return True
    return pid_alive(record.pid)


def list_sessions(
    *,
    include_stale: bool = False,
    stale_after: float = DEFAULT_STALE_AFTER,
    registry_dir: Path | None = None,
) -> list[SessionRecord]:
    """Records on this machine, newest heartbeat first."""
    directory = registry_dir or default_registry_dir()
    if not directory.is_dir():
        return []
    records: list[SessionRecord] = []
    for path in directory.glob("*.json"):
        record = load_session(path.stem, registry_dir=directory)
        if record is None:
            continue
        if include_stale or is_live(record, stale_after=stale_after):
            records.append(record)
    return sorted(records, key=lambda r: -r.heartbeat)


def prune_stale(*, stale_after: float = DEFAULT_STALE_AFTER, registry_dir: Path | None = None) -> int:
    """Delete records whose host is gone; returns how many were removed."""
    removed = 0
    for record in list_sessions(include_stale=True, stale_after=stale_after, registry_dir=registry_dir):
        if not is_live(record, stale_after=stale_after) and unregister(record.session_id, registry_dir=registry_dir):
            removed += 1
    return removed


def find_session(name_or_id: str, *, registry_dir: Path | None = None, stale_after: float = DEFAULT_STALE_AFTER) -> SessionRecord:
    """Resolve a session by exact id, exact name, or a unique prefix of either.

    Raises:
        LookupError: When nothing matches, or the prefix is ambiguous.
    """
    wanted = name_or_id.strip()
    live = list_sessions(stale_after=stale_after, registry_dir=registry_dir)
    for record in live:
        if record.session_id == wanted or (record.name and record.name == wanted):
            return record
    prefix = [r for r in live if r.session_id.startswith(wanted) or (r.name and r.name.startswith(wanted))]
    if len(prefix) == 1:
        return prefix[0]
    if not prefix:
        msg = f"no live session named {wanted!r}; `bog-agents sessions` lists them"
        raise LookupError(msg)
    names = ", ".join(r.label for r in prefix)
    msg = f"{wanted!r} matches several sessions: {names}"
    raise LookupError(msg)


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    for limit, divisor, unit in _AGE_UNITS:
        if seconds < limit:
            return f"{int(seconds // divisor)}{unit}"
    return f"{int(seconds // 86400)}d"


def format_sessions(records: list[SessionRecord], *, now: float | None = None) -> str:
    """A table for `bog-agents sessions`."""
    if not records:
        return "No live sessions on this machine."
    now = now if now is not None else time.time()
    header = ("SESSION", "KIND", "STATE", "AGE", "PID", "MODEL", "CWD")
    rows = [
        (
            r.label[:24],
            r.kind,
            r.state,
            _age(now - r.heartbeat),
            str(r.pid),
            (r.model or "-")[:28],
            r.cwd or "-",
        )
        for r in records
    ]
    widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header) - 1)]
    lines = ["  ".join(header[i].ljust(widths[i]) for i in range(len(widths))) + "  " + header[-1]]
    lines.extend("  ".join(row[i].ljust(widths[i]) for i in range(len(widths))) + "  " + row[-1] for row in rows)
    return "\n".join(lines)


with contextlib.suppress(Exception):  # keep the module importable on exotic platforms
    pid_alive(os.getpid())


__all__ = [
    "DEFAULT_STALE_AFTER",
    "STATES",
    "SessionRecord",
    "default_registry_dir",
    "find_session",
    "format_sessions",
    "heartbeat",
    "is_live",
    "list_sessions",
    "load_session",
    "pid_alive",
    "prune_stale",
    "register",
    "unregister",
]
