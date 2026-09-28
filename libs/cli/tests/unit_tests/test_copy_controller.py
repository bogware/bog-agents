"""Tests for `bog_agents_cli.copy_controller` (the /copy command logic)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bog_agents_cli.copy_controller import build_copy_payload, parse_copy_mode


class TestBuildCopyPayload:
    def test_empty_conversation(self) -> None:
        text, notify = build_copy_payload([], "last")
        assert text is None
        assert "empty" in notify.lower()

    def test_last_assistant_response(self) -> None:
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello there")]
        text, notify = build_copy_payload(msgs, "last")
        assert text == "hello there"
        assert "last response" in notify.lower()

    def test_last_skips_trailing_non_ai(self) -> None:
        # A tool/human message after the AI reply must not hide it.
        msgs = [
            AIMessage(content="the answer is 42"),
            ToolMessage(content="tool output", tool_call_id="t1"),
        ]
        text, _ = build_copy_payload(msgs, "last")
        assert text == "the answer is 42"

    def test_last_with_no_assistant(self) -> None:
        text, notify = build_copy_payload([HumanMessage(content="hi")], "last")
        assert text is None
        assert "no assistant response" in notify.lower()

    def test_all_transcript(self) -> None:
        msgs = [HumanMessage(content="q1"), AIMessage(content="a1")]
        text, notify = build_copy_payload(msgs, "all")
        assert text is not None
        assert "## You\nq1" in text
        assert "## Assistant\na1" in text
        assert "transcript" in notify.lower()

    def test_dict_messages_remote_mode(self) -> None:
        # The remote LangGraph server returns plain dicts.
        msgs = [
            {"type": "human", "content": "hi"},
            {"type": "ai", "content": "dict reply"},
        ]
        text, _ = build_copy_payload(msgs, "last")
        assert text == "dict reply"

    def test_content_block_list_extraction(self) -> None:
        msgs = [
            AIMessage(
                content=[
                    {"type": "text", "text": "part one "},
                    {"type": "text", "text": "part two"},
                ]
            )
        ]
        text, _ = build_copy_payload(msgs, "last")
        assert text == "part one part two"


class TestParseCopyMode:
    def test_default_is_last(self) -> None:
        assert parse_copy_mode("/copy") == "last"

    def test_all(self) -> None:
        assert parse_copy_mode("/copy all") == "all"
        assert parse_copy_mode("/copy transcript") == "all"

    def test_unknown_arg_defaults_last(self) -> None:
        assert parse_copy_mode("/copy something") == "last"
