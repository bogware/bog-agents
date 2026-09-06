"""ROADMAP #76: typed attachments on the team mailbox and the send / receive tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bog_agents.mailbox_store import MailboxStore
from bog_agents.teams import Attachment, Mailbox, Message
from bog_agents.tools import team_files as tf


def _tools(mailbox: Any, member: str, root: Path, *, audit: list[dict[str, Any]] | None = None, git: Any = None) -> dict[str, Any]:
    sink = (lambda kind, data: audit.append({"kind": kind, **data})) if audit is not None else None
    return {t.name: t for t in tf.team_file_tools(mailbox, member, root=root, audit=sink, run_git=git)}


def test_attachment_round_trip_on_both_mailboxes(tmp_path: Path) -> None:
    attachment = Attachment(kind="file", name="a.txt", path=str(tmp_path / "a.txt"), sha256="sha256:abc", size=3)
    for mailbox in (Mailbox(), MailboxStore(tmp_path / "mail.db")):
        sent = mailbox.send("alice", "bob", "here", attachments=(attachment,))
        assert sent.attachments == (attachment,)
        assert mailbox.inbox("bob")[0].attachments[0].to_dict()["sha256"] == "sha256:abc"
        drained = mailbox.drain("bob")
        assert drained[0].attachments == (attachment,) and mailbox.drain("bob") == []
    assert Attachment.from_dict(attachment.to_dict()) == attachment
    assert Message(sender="a", recipient="b", body="x").attachments == ()


def test_send_file_scans_and_receive_copies(tmp_path: Path) -> None:
    mailbox = Mailbox()
    audit: list[dict[str, Any]] = []
    (tmp_path / "notes.txt").write_text(
        "token sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ and email me@example.com\n", encoding="utf-8"
    )
    alice = _tools(mailbox, "alice", tmp_path, audit=audit)
    bob = _tools(mailbox, "bob", tmp_path, audit=audit)

    out = alice["send_file"].invoke({"recipient": "bob", "path": "notes.txt", "note": "for the fixture"})
    assert out.startswith("Sent file notes.txt") and "redacted" in out
    staged = Path(mailbox.inbox("bob")[0].attachments[0].path)
    assert "me@example.com" not in staged.read_text(encoding="utf-8") and staged.parent.parent == tf.exchange_dir(tmp_path)
    assert audit[0]["kind"] == "team_file" and audit[0]["attachment_kind"] == "file" and audit[0]["from"] == "alice"
    assert audit[0]["redactions"] >= 1

    received = bob["receive_files"].invoke({})
    assert "file notes.txt from alice" in received and "for the fixture" in received
    delivered = tf.inbox_dir(tmp_path, "bob") / "notes.txt"
    assert delivered.is_file() and "[EMAIL-REDACTED]" in delivered.read_text(encoding="utf-8")
    assert bob["receive_files"].invoke({}) == "No new files from teammates."
    assert alice["receive_files"].invoke({}) == "No new files from teammates."  # a sender never receives its own file

    assert alice["send_file"].invoke({"recipient": "bob", "path": "../outside.txt"}).startswith("Error:")
    assert alice["send_file"].invoke({"recipient": "bob", "path": "missing.txt"}).startswith("Error:")


def test_send_directory_zips_and_skips_junk(tmp_path: Path) -> None:
    mailbox = Mailbox()
    (tmp_path / "fixtures" / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "fixtures" / "node_modules" / "x" / "big.js").write_text("junk", encoding="utf-8")
    (tmp_path / "fixtures" / "data.json").write_text("{}", encoding="utf-8")
    alice = _tools(mailbox, "alice", tmp_path)
    bob = _tools(mailbox, "bob", tmp_path)
    assert alice["send_file"].invoke({"recipient": "@all", "path": "fixtures"}).startswith("Sent dir fixtures.zip")
    listing = bob["receive_files"].invoke({"dest": "incoming"})
    assert "dir fixtures.zip from alice" in listing
    assert (tmp_path / "incoming" / "fixtures" / "data.json").is_file() and not (tmp_path / "incoming" / "fixtures" / "node_modules").exists()


def test_send_patch_uses_git_and_untracked(tmp_path: Path) -> None:
    mailbox = Mailbox()
    (tmp_path / "new.py").write_text("print('hi')\n", encoding="utf-8")
    calls: list[list[str]] = []

    def _git(_repo: Path, args: list[str]) -> str:
        calls.append(args)
        if args[0] == "diff":
            return "diff --git a/old.py b/old.py\n--- a/old.py\n+++ b/old.py\n@@ -1 +1 @@\n-x\n+y\n"
        return "new.py\n"

    alice = _tools(mailbox, "alice", tmp_path, git=_git)
    out = alice["send_patch"].invoke({"recipient": "bob", "note": "my half"})
    assert out.startswith("Sent patch alice-") and calls[0] == ["diff", "HEAD", "--binary"]
    patch = Path(mailbox.inbox("bob")[0].attachments[0].path).read_text(encoding="utf-8")
    assert "+++ b/old.py" in patch and "diff --git a/new.py b/new.py" in patch and "+print('hi')" in patch

    empty = _tools(Mailbox(), "carol", tmp_path, git=lambda _r, _a: "")
    assert empty["send_patch"].invoke({"recipient": "bob"}) == "Nothing to send: the working tree matches HEAD."
