"""Unit tests for tool-call capture in non-interactive structured output.

Exercises `_process_ai_message` / `StreamState` directly (no LangGraph server)
to confirm the agent's tool calls are recorded for the `--json` envelope and
de-duplicated by tool-call id.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console

from bog_agents_cli.file_ops import FileOpTracker
from bog_agents_cli.non_interactive import (
    StreamState,
    _process_ai_message,
    _process_message_chunk,
)

if TYPE_CHECKING:
    import pytest


def _ai_message_with_tool_calls(tool_calls: list[dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _parse_jsonl(captured: str) -> list[dict]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def test_tool_calls_recorded_in_state() -> None:
    """Completed tool calls land in `state.tool_calls` as name/args dicts."""
    state = StreamState()
    console = Console(stderr=True)
    msg = _ai_message_with_tool_calls(
        [
            {
                "name": "read_file",
                "args": {"file_path": "/a.txt"},
                "id": "c1",
                "type": "tool_call",
            },
            {
                "name": "write_file",
                "args": {"file_path": "/b.txt", "content": "x"},
                "id": "c2",
                "type": "tool_call",
            },
        ]
    )
    _process_ai_message(msg, state, console)

    assert [tc["name"] for tc in state.tool_calls] == ["read_file", "write_file"]
    assert state.tool_calls[0]["args"] == {"file_path": "/a.txt"}


def test_tool_calls_deduped_by_id() -> None:
    """The same tool-call id is not recorded twice across message chunks."""
    state = StreamState()
    console = Console(stderr=True)
    tc = {"name": "ls", "args": {"path": "/"}, "id": "dup", "type": "tool_call"}
    _process_ai_message(_ai_message_with_tool_calls([tc]), state, console)
    _process_ai_message(_ai_message_with_tool_calls([tc]), state, console)

    assert len(state.tool_calls) == 1
    assert state.tool_calls[0]["name"] == "ls"


def test_no_tool_calls_leaves_list_empty() -> None:
    """A plain text AIMessage records no tool calls."""
    state = StreamState()
    console = Console(stderr=True)
    _process_ai_message(AIMessage(content="just text"), state, console)
    assert state.tool_calls == []


def test_jsonl_emits_text_event(capsys: pytest.CaptureFixture[str]) -> None:
    """In jsonl mode, text content is emitted as a `text` event (not raw text)."""
    state = StreamState(output_format="jsonl")
    console = Console(stderr=True)
    _process_ai_message(AIMessage(content="hello world"), state, console)
    events = _parse_jsonl(capsys.readouterr().out)
    assert {"type": "text", "text": "hello world"} in events


def test_jsonl_emits_tool_call_event(capsys: pytest.CaptureFixture[str]) -> None:
    """In jsonl mode, a completed tool call is emitted as a `tool_call` event."""
    state = StreamState(output_format="jsonl")
    console = Console(stderr=True)
    msg = _ai_message_with_tool_calls(
        [
            {
                "name": "read_file",
                "args": {"file_path": "/a"},
                "id": "c1",
                "type": "tool_call",
            }
        ]
    )
    _process_ai_message(msg, state, console)
    events = _parse_jsonl(capsys.readouterr().out)
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    assert tool_calls == [
        {"type": "tool_call", "name": "read_file", "args": {"file_path": "/a"}}
    ]


def test_jsonl_emits_tool_result_event(capsys: pytest.CaptureFixture[str]) -> None:
    """In jsonl mode, a ToolMessage is emitted as a `tool_result` event."""
    state = StreamState(output_format="jsonl")
    console = Console(stderr=True)
    tracker = FileOpTracker(assistant_id="test", backend=None)
    tool_msg = ToolMessage(
        content="file contents", name="read_file", tool_call_id="c1", status="success"
    )
    _process_message_chunk((tool_msg, {}), state, console, tracker)
    events = _parse_jsonl(capsys.readouterr().out)
    results = [e for e in events if e.get("type") == "tool_result"]
    assert len(results) == 1
    assert results[0]["name"] == "read_file"
    assert results[0]["status"] == "success"
    assert "file contents" in results[0]["content"]
