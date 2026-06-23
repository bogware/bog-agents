"""Hardening tests for session_fork persistence (S10).

Verifies the fork record is persisted as UTF-8 even when non-ASCII content
is present, guarding against the latent encoding-convention violation where
``write_text`` was called without an explicit ``encoding``.
"""

from __future__ import annotations

import json
from pathlib import Path

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
