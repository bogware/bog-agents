"""Hardening tests for session_fork persistence (S10).

Verifies the fork record is persisted as UTF-8 even when non-ASCII content
is present, guarding against the latent encoding-convention violation where
``write_text`` was called without an explicit ``encoding``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from bog_agents_cli import session_fork
from bog_agents_cli.session_fork import create_fork, list_forks


def test_save_fork_writes_utf8(tmp_path: Path) -> None:
    """Non-ASCII fork metadata round-trips through a UTF-8 encoded file."""
    fork = create_fork(
        tmp_path,
        "thread-1",
        name="Brücke — 日本語",
        description="explore café branch ✨",
        message_count=3,
    )

    forks_file = tmp_path / "forks" / "thread-1.json"
    assert forks_file.exists()

    # Decoded explicitly as UTF-8: a cp1252/cp932 default write would have
    # raised here or produced mojibake.
    raw = forks_file.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data[0]["name"] == "Brücke — 日本語"
    assert data[0]["description"] == "explore café branch ✨"
    assert data[0]["fork_id"] == fork.fork_id


def test_save_fork_utf8_bytes_on_disk(tmp_path: Path) -> None:
    """The on-disk bytes are valid UTF-8 regardless of platform default."""
    create_fork(tmp_path, "thread-2", name="naïve", message_count=1)

    forks_file = tmp_path / "forks" / "thread-2.json"
    # Will raise UnicodeDecodeError if written with a non-UTF-8 codec.
    forks_file.read_bytes().decode("utf-8")


def test_round_trip_through_list_forks(tmp_path: Path) -> None:
    """Forks written with non-ASCII data load back via list_forks."""
    create_fork(tmp_path, "thread-3", name="Über-fork", description="ümlaut")
    create_fork(tmp_path, "thread-3", name="second", description="plain")

    forks = list_forks(tmp_path, "thread-3")
    assert [f.name for f in forks] == ["Über-fork", "second"]
    assert forks[0].description == "ümlaut"


def test_concurrent_forks_preserve_all_records(tmp_path: Path) -> None:
    """Concurrent forks of one parent never lose a record (P30 lost-write).

    Spawns many threads that each fork the same parent thread simultaneously.
    Before the lock, the read-modify-write in ``_save_fork`` interleaved and the
    later writer clobbered the earlier writer's appended entry. With the lock
    every record survives.
    """
    n_writers = 24
    parent = "race-parent"
    start = threading.Barrier(n_writers)

    def worker(idx: int) -> None:
        start.wait()  # maximize interleaving on the read-modify-write
        create_fork(tmp_path, parent, name=f"fork-{idx}", message_count=idx)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    forks = list_forks(tmp_path, parent)
    assert len(forks) == n_writers, f"expected {n_writers} records, got {len(forks)}"
    # Every distinct writer's record is present (no silent overwrite).
    assert {f.name for f in forks} == {f"fork-{i}" for i in range(n_writers)}
    # Fork ids are unique — no record was partially merged.
    assert len({f.fork_id for f in forks}) == n_writers


def test_simulated_two_process_append_preserves_both(tmp_path: Path) -> None:
    """Two writers reading the same base both land in the final file.

    Drives ``_save_fork`` directly from two threads guarded only by the on-disk
    lock (the in-process lock is bypassed per writer by using distinct lock
    registry keys would be wrong — instead we assert the real combined lock
    serializes them). This is the closest single-process analogue of two CLI
    processes racing the index.
    """
    parent = "twoproc"
    forks_a = session_fork.SessionFork(
        fork_id="aaaa1111",
        parent_thread_id=parent,
        fork_thread_id=f"{parent}-fork-aaaa1111",
        name="writer-a",
    )
    forks_b = session_fork.SessionFork(
        fork_id="bbbb2222",
        parent_thread_id=parent,
        fork_thread_id=f"{parent}-fork-bbbb2222",
        name="writer-b",
    )

    barrier = threading.Barrier(2)

    def save(fork: session_fork.SessionFork) -> None:
        barrier.wait()
        session_fork._save_fork(tmp_path, fork)

    ta = threading.Thread(target=save, args=(forks_a,))
    tb = threading.Thread(target=save, args=(forks_b,))
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    forks = list_forks(tmp_path, parent)
    assert {f.fork_id for f in forks} == {"aaaa1111", "bbbb2222"}


def test_lock_file_cleaned_up(tmp_path: Path) -> None:
    """The on-disk lock file is removed after a successful save."""
    create_fork(tmp_path, "cleanup", name="only")

    forks_file = tmp_path / "forks" / "cleanup.json"
    lock_file = forks_file.with_suffix(forks_file.suffix + ".lock")
    assert forks_file.exists()
    assert not lock_file.exists()


def test_stale_lock_does_not_wedge(tmp_path: Path, monkeypatch) -> None:
    """A leftover lock file from a crashed peer does not hang the save forever.

    Shrinks the acquisition timeout so the test is fast, pre-creates the lock
    file (simulating a crashed writer that never cleaned up), and asserts the
    fork is still persisted (bounded-race fallback).
    """
    monkeypatch.setattr(session_fork, "_LOCK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(session_fork, "_LOCK_POLL_SECONDS", 0.01)

    forks_dir = tmp_path / "forks"
    forks_dir.mkdir(parents=True, exist_ok=True)
    forks_file = forks_dir / "stale.json"
    lock_file = forks_file.with_suffix(forks_file.suffix + ".lock")
    lock_file.write_text("", encoding="utf-8")  # never released

    create_fork(tmp_path, "stale", name="survivor")

    forks = list_forks(tmp_path, "stale")
    assert [f.name for f in forks] == ["survivor"]
