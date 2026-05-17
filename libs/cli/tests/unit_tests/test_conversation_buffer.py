"""Tests for the CLI-wide conversation buffer (Wave H)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bog_agents_cli.conversation_buffer import (
    ConversationBuffer,
    get_buffer,
    recent_messages,
    reset_buffers,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated() -> None:
    reset_buffers()


class TestBuffer:
    def test_record_and_recent(self) -> None:
        buf = ConversationBuffer()
        buf.record(role="user", content="hello")
        buf.record(role="assistant", content="hi there")
        assert len(buf) == 2
        entries = buf.recent(5)
        assert [e.role for e in entries] == ["user", "assistant"]
        assert entries[1].content == "hi there"

    def test_empty_content_skipped(self) -> None:
        buf = ConversationBuffer()
        buf.record(role="user", content="")
        assert len(buf) == 0

    def test_bounded(self) -> None:
        buf = ConversationBuffer(max_entries=3)
        for i in range(10):
            buf.record(role="user", content=f"turn {i}")
        assert len(buf) == 3
        entries = buf.recent(5)
        # FIFO eviction — only the last 3 should remain.
        assert [e.content for e in entries] == ["turn 7", "turn 8", "turn 9"]

    def test_recent_limit_zero_returns_empty(self) -> None:
        buf = ConversationBuffer()
        buf.record(role="user", content="x")
        assert buf.recent(0) == []

    def test_clear(self) -> None:
        buf = ConversationBuffer()
        buf.record(role="user", content="x")
        buf.clear()
        assert len(buf) == 0


class TestPerCwdRegistry:
    def test_distinct_buffers_per_cwd(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ba = get_buffer(a)
        bb = get_buffer(b)
        assert ba is not bb
        ba.record(role="user", content="for a")
        bb.record(role="user", content="for b")
        # Each buffer holds only its own entry.
        assert [e.content for e in ba.recent()] == ["for a"]
        assert [e.content for e in bb.recent()] == ["for b"]

    def test_same_cwd_returns_cached(self, tmp_path: Path) -> None:
        first = get_buffer(tmp_path)
        second = get_buffer(tmp_path)
        assert first is second


class TestRecentMessages:
    def test_default_filter_drops_app(self, tmp_path: Path) -> None:
        buf = get_buffer(tmp_path)
        buf.record(role="user", content="ask")
        buf.record(role="app", content="status")
        buf.record(role="assistant", content="answer")
        msgs = recent_messages(tmp_path)
        # "app" entries should be dropped by default.
        assert len(msgs) == 2
        assert isinstance(msgs[0], HumanMessage)
        assert isinstance(msgs[1], AIMessage)

    def test_include_roles_override(self, tmp_path: Path) -> None:
        buf = get_buffer(tmp_path)
        buf.record(role="user", content="u")
        buf.record(role="app", content="a")
        msgs = recent_messages(tmp_path, include_roles=("user", "app"))
        assert len(msgs) == 2

    def test_tool_role_maps_to_toolmessage(self, tmp_path: Path) -> None:
        buf = get_buffer(tmp_path)
        buf.record(role="tool", content="tool output")
        msgs = recent_messages(tmp_path)
        assert len(msgs) == 1
        assert isinstance(msgs[0], ToolMessage)


class TestSidecarIntegration:
    def test_sidecar_auto_picks_up_buffer(self, tmp_path: Path) -> None:
        """Sidecar pulls from the conversation buffer by default.

        When neither parent_messages nor context_override is supplied
        the controller falls back to recent_messages(cwd).
        """
        from bog_agents_cli.sidecar_controller import (
            SidecarController,
            reset_controllers,
        )

        reset_controllers()
        # Seed the buffer with the parent's recent exchange.
        get_buffer(tmp_path).record(role="user", content="What is alpha?")
        get_buffer(tmp_path).record(role="assistant", content="alpha is X")

        # Stub model: capture the messages it sees so we can verify the
        # context summary made it through.
        seen_messages: list = []

        class _Stub:
            def bind_tools(self, _t: list) -> _Stub:
                return self

            def invoke(self, messages: list) -> object:
                seen_messages.extend(messages)
                return AIMessage(content="answered")

        c = SidecarController(
            working_dir=tmp_path,
            model_factory=lambda: _Stub(),
            web_search=False,
        )
        result = c.run("what did the parent learn?")
        assert result.ok
        # The HumanMessage the sidecar saw should include both the
        # original Q and the answer from the buffer.
        human_block = next(
            (m.content for m in seen_messages if isinstance(m, HumanMessage)),
            "",
        )
        assert "What is alpha?" in human_block
        assert "alpha is X" in human_block
