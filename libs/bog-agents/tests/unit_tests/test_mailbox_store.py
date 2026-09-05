"""ROADMAP #56: the SQLite mailbox mirrors `teams.Mailbox` across processes."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from bog_agents.mailbox_store import MailboxStore
from bog_agents.teams import Mailbox


def _behaviour(box: Mailbox | MailboxStore) -> list[list[str]]:
    box.send("alice", "bob", "hi bob")
    box.send("alice", "@all", "hello everyone")
    box.send("bob", "alice", "hi alice")
    out = [
        [m.body for m in box.inbox("bob")],
        [m.body for m in box.drain("bob")],
        [m.body for m in box.drain("bob")],  # second drain: nothing new
        [m.body for m in box.drain("alice")],  # alice never sees her own broadcast
        [m.body for m in box.inbox("carol")],  # broadcast only
    ]
    box.send("carol", "bob", "late")
    out.append([m.body for m in box.drain("bob")])
    return out


def test_same_semantics_as_in_memory_mailbox(tmp_path: Path) -> None:
    assert _behaviour(MailboxStore(tmp_path / "mail.db")) == _behaviour(Mailbox())


def test_pending_counts_and_purge(tmp_path: Path) -> None:
    store = MailboxStore(tmp_path / "mail.db")
    store.send("cli", "tui", "one")
    store.send("cli", "tui", "two")
    assert store.pending("tui") == 2
    assert [m.body for m in store.drain("tui")] == ["one", "two"]
    assert store.pending("tui") == 0
    assert store.purge(older_than=0.0) == 2
    assert store.inbox("tui") == []


def test_wait_returns_early_when_a_message_lands(tmp_path: Path) -> None:
    store = MailboxStore(tmp_path / "mail.db")
    started = time.monotonic()
    assert store.wait("nobody", timeout=0.2, poll=0.05) == []
    assert time.monotonic() - started >= 0.2
    store.send("a", "b", "now")
    assert [m.body for m in store.wait("b", timeout=5.0)] == ["now"]


def test_cross_process_enqueue_and_drain(tmp_path: Path) -> None:
    db = tmp_path / "mail.db"
    store = MailboxStore(db)
    script = (
        "import sys; from bog_agents.mailbox_store import MailboxStore; "
        "s = MailboxStore(sys.argv[1]); s.send('other-process', 'tui', 'from afar'); "
        "print(s.pending('tui'))"
    )
    result = subprocess.run([sys.executable, "-c", script, str(db)], capture_output=True, text=True, check=True, timeout=60)
    assert result.stdout.strip() == "1"
    got = store.drain("tui")
    assert [(m.sender, m.body) for m in got] == [("other-process", "from afar")]
    assert store.drain("tui") == []
