"""Session forking — branch conversations into parallel paths.

Feature #22: Session forking — snapshot the current conversation state
and create a new branch to explore alternative approaches.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)

# Per-path in-process locks guard concurrent forks of the same parent within a
# single CLI process; the on-disk lock file (below) guards the cross-process case.
_INPROC_LOCKS: dict[str, threading.Lock] = {}
_INPROC_LOCKS_GUARD = threading.Lock()

# Cross-process lock acquisition tuning. The append-under-lock critical section
# is tiny (read a small JSON file, append one record, atomic rename), so a brief
# spin with a hard ceiling is sufficient and avoids hanging the CLI forever if a
# stale lock file is left behind by a crashed peer.
_LOCK_POLL_SECONDS = 0.02
_LOCK_TIMEOUT_SECONDS = 10.0


def _inproc_lock_for(key: str) -> threading.Lock:
    """Return the process-wide :class:`threading.Lock` for *key*.

    Args:
        key: Stable identity for the resource (the forks file path).

    Returns:
        A lock shared by every caller using the same *key* in this process.
    """
    with _INPROC_LOCKS_GUARD:
        lock = _INPROC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INPROC_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _fork_file_lock(forks_file: Path) -> Iterator[None]:
    """Serialize read-modify-write of *forks_file* across threads and processes.

    Combines a per-path in-process :class:`threading.Lock` with an on-disk lock
    file acquired via ``O_CREAT | O_EXCL`` so that two concurrent CLI processes
    forking the same parent thread cannot interleave their read+append+write and
    lose a record. The lock file lives next to *forks_file* (``<file>.lock``).

    If the lock cannot be acquired within ``_LOCK_TIMEOUT_SECONDS`` the lock is
    assumed stale (a peer crashed mid-write) and the body proceeds anyway — a
    bounded race is preferable to wedging the CLI indefinitely.

    Args:
        forks_file: The per-parent forks JSON file being mutated.

    Yields:
        None, with both locks held for the duration of the ``with`` block.
    """
    inproc = _inproc_lock_for(str(forks_file))
    lock_path = forks_file.with_suffix(forks_file.suffix + ".lock")
    inproc.acquire()
    have_file_lock = False
    fd: int | None = None
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                have_file_lock = True
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Timed out acquiring fork lock %s; proceeding without it (possible stale lock)",
                        lock_path,
                    )
                    break
                time.sleep(_LOCK_POLL_SECONDS)
            except OSError as exc:
                # Locking is best-effort hardening, not correctness-critical on
                # exotic filesystems; degrade to the in-process lock only.
                logger.debug("Could not create fork lock %s: %s", lock_path, exc)
                break
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if have_file_lock:
            with contextlib.suppress(OSError):
                lock_path.unlink()
        inproc.release()


@dataclass
class SessionFork:
    """A forked session branch."""

    fork_id: str
    """Unique identifier for this fork."""

    parent_thread_id: str
    """Thread ID this fork was created from."""

    fork_thread_id: str
    """New thread ID for the forked session."""

    name: str = ""
    """Human-readable name for the fork."""

    description: str = ""
    """Why this fork was created."""

    created_at: float = 0.0
    """Timestamp when the fork was created."""

    message_count: int = 0
    """Number of messages at fork point."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata."""


def create_fork(
    config_dir: Path,
    parent_thread_id: str,
    *,
    name: str = "",
    description: str = "",
    message_count: int = 0,
) -> SessionFork:
    """Create a new session fork.

    Args:
        config_dir: Config directory.
        parent_thread_id: Current thread ID.
        name: Name for the fork.
        description: Description of fork purpose.
        message_count: Current message count.

    Returns:
        SessionFork with new thread ID.
    """
    import uuid

    fork_id = str(uuid.uuid4())[:8]
    fork_thread_id = f"{parent_thread_id}-fork-{fork_id}"

    fork = SessionFork(
        fork_id=fork_id,
        parent_thread_id=parent_thread_id,
        fork_thread_id=fork_thread_id,
        name=name or f"Fork {fork_id}",
        description=description,
        created_at=time.time(),
        message_count=message_count,
    )

    _save_fork(config_dir, fork)
    return fork


def _save_fork(config_dir: Path, fork: SessionFork) -> None:
    """Save a fork record to disk.

    Args:
        config_dir: Config directory.
        fork: Fork to save.
    """
    forks_dir = config_dir / "forks"
    forks_dir.mkdir(parents=True, exist_ok=True)

    forks_file = forks_dir / f"{fork.parent_thread_id}.json"

    # The read+append+write below must be atomic with respect to other forkers
    # of the same parent (in-process and cross-process). Without the lock two
    # writers both read the same `existing`, each append one record, and the
    # later `atomic_write_text` clobbers the earlier — a silent lost write.
    with _fork_file_lock(forks_file):
        existing: list[dict[str, Any]] = []

        if forks_file.exists():
            try:
                existing = json.loads(forks_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        existing.append(
            {
                "fork_id": fork.fork_id,
                "parent_thread_id": fork.parent_thread_id,
                "fork_thread_id": fork.fork_thread_id,
                "name": fork.name,
                "description": fork.description,
                "created_at": fork.created_at,
                "message_count": fork.message_count,
                "metadata": fork.metadata,
            }
        )

        atomic_write_text(forks_file, json.dumps(existing, indent=2), encoding="utf-8")


def list_forks(config_dir: Path, thread_id: str) -> list[SessionFork]:
    """List all forks for a thread.

    Args:
        config_dir: Config directory.
        thread_id: Thread to list forks for.

    Returns:
        List of SessionFork instances.
    """
    forks_file = config_dir / "forks" / f"{thread_id}.json"
    if not forks_file.exists():
        return []

    try:
        data = json.loads(forks_file.read_text(encoding="utf-8"))
        return [
            SessionFork(
                fork_id=f["fork_id"],
                parent_thread_id=f["parent_thread_id"],
                fork_thread_id=f["fork_thread_id"],
                name=f.get("name", ""),
                description=f.get("description", ""),
                created_at=f.get("created_at", 0),
                message_count=f.get("message_count", 0),
                metadata=f.get("metadata", {}),
            )
            for f in data
        ]
    except (json.JSONDecodeError, OSError, KeyError) as e:
        logger.warning("Failed to load forks for %s: %s", thread_id, e)
        return []


def format_forks(forks: list[SessionFork]) -> str:
    """Format forks for display.

    Args:
        forks: List of forks.

    Returns:
        Formatted string.
    """
    if not forks:
        return "No forks for this session."

    lines = ["## Session Forks\n"]
    for fork in forks:
        lines.append(f"- **{fork.name}** ({fork.fork_id})")
        if fork.description:
            lines.append(f"  {fork.description}")
        lines.append(f"  Thread: {fork.fork_thread_id}")
        lines.append(f"  Messages at fork: {fork.message_count}")
    return "\n".join(lines)
