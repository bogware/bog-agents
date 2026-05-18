"""Persistence guarantees for :class:`MessageStore`.

Crash recovery is opt-in via ``persist_path=``. When wired, every
``append()`` lands on disk before the call returns; a fresh process can
read the file back with :meth:`MessageStore.replay_from_persist` and
re-hydrate the transcript. We assert both halves of that contract here.
"""

from __future__ import annotations

import json
from pathlib import Path

from bog_agents_cli.widgets.message_store import (
    MessageData,
    MessageStore,
    MessageType,
    ToolStatus,
)


class TestPersistRoundTrip:
    def test_append_writes_jsonl_line_eagerly(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        store = MessageStore(persist_path=path)
        store.append(MessageData(type=MessageType.USER, content="hello world"))
        store.close_persist()

        lines = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        assert lines[0]["event"] == "append"
        assert lines[0]["data"]["content"] == "hello world"
        assert lines[0]["data"]["type"] == "user"

    def test_replay_reconstructs_messages_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        store = MessageStore(persist_path=path)
        store.append(MessageData(type=MessageType.USER, content="ask"))
        store.append(MessageData(type=MessageType.ASSISTANT, content="answer"))
        store.append(
            MessageData(
                type=MessageType.TOOL,
                content="",
                tool_name="execute",
                tool_status=ToolStatus.SUCCESS,
                tool_output="ok",
            )
        )
        store.close_persist()

        recovered = MessageStore.replay_from_persist(path)
        assert [m.content for m in recovered] == ["ask", "answer", ""]
        assert [m.type for m in recovered] == [
            MessageType.USER,
            MessageType.ASSISTANT,
            MessageType.TOOL,
        ]
        assert recovered[2].tool_name == "execute"
        assert recovered[2].tool_status is ToolStatus.SUCCESS

    def test_replay_skips_corrupt_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        store = MessageStore(persist_path=path)
        store.append(MessageData(type=MessageType.USER, content="good"))
        store.close_persist()

        # Splice in a corrupt line between good entries — simulates a
        # crash mid-write.
        with path.open("a", encoding="utf-8") as fh:
            fh.write("THIS IS NOT JSON\n")
        store2 = MessageStore(persist_path=path)
        store2.append(MessageData(type=MessageType.USER, content="also good"))
        store2.close_persist()

        recovered = MessageStore.replay_from_persist(path)
        assert [m.content for m in recovered] == ["good", "also good"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert MessageStore.replay_from_persist(tmp_path / "absent.jsonl") == []

    def test_no_persist_path_is_back_compat(self, tmp_path: Path) -> None:
        """Constructing without persist_path must not touch the disk."""
        store = MessageStore()
        store.append(MessageData(type=MessageType.USER, content="x"))
        # No file should have been created anywhere; just verify the
        # store still works in memory.
        assert store.total_count == 1
        store.close_persist()  # idempotent no-op
