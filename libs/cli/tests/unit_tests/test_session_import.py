"""ROADMAP #62: session import (Claude Code / Codex / Cline) and com.bogware.thread export."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bog_agents_cli import session_import as si, sessions


def _claude_jsonl(path: Path) -> None:
    lines = [
        {"type": "summary", "summary": "Fix the ratchet"},
        {
            "type": "user",
            "cwd": "E:/proj",
            "timestamp": "2026-09-01T10:00:00Z",
            "message": {"role": "user", "content": "Please fix the ratchet test"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "On it."},
                    {"type": "tool_use", "name": "Bash"},
                ],
            },
        },
        {
            "type": "user",
            "isMeta": True,
            "message": {"role": "user", "content": "meta"},
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:01:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "ok"}],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "Done."}},
    ]
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def test_parse_claude_code(tmp_path: Path) -> None:
    path = tmp_path / "abc.jsonl"
    _claude_jsonl(path)
    thread = si.parse_claude_code_jsonl(path)
    assert thread is not None
    assert thread.title == "Fix the ratchet"
    assert thread.cwd == "E:/proj"
    assert [(m.role, m.text) for m in thread.messages] == [
        ("user", "Please fix the ratchet test"),
        ("assistant", "On it."),
        ("assistant", "Done."),
    ]
    assert thread.created_at is not None
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert si.parse_claude_code_jsonl(tmp_path / "empty.jsonl") is None


def test_parse_codex_and_cline(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout-1.jsonl"
    lines = [
        {
            "type": "session_meta",
            "payload": {"cwd": "/repo", "timestamp": "2026-09-02T01:00:00Z"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-02T01:00:01Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "add tests"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "sure"}],
            },
        },
        {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "hidden"}],
        },
    ]
    rollout.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    thread = si.parse_codex_rollout(rollout)
    assert thread is not None
    assert thread.cwd == "/repo"
    assert [m.role for m in thread.messages] == ["user", "assistant"]
    assert thread.title == "add tests"

    task = tmp_path / "1725000000000"
    task.mkdir()
    (task / "api_conversation_history.json").write_text(
        json.dumps(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "<task>\nrefactor\n</task>\n<environment_details>stuff</environment_details>",
                        }
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            ]
        ),
        encoding="utf-8",
    )
    cline = si.parse_cline_task(task)
    assert cline is not None
    assert cline.messages[0].text == "refactor"
    assert cline.created_at == 1725000000.0


def test_discovery_orders_newest_first(tmp_path: Path) -> None:
    projects = tmp_path / ".claude" / "projects" / "slug"
    projects.mkdir(parents=True)
    old, new = projects / "old.jsonl", projects / "new.jsonl"
    _claude_jsonl(old)
    _claude_jsonl(new)
    import os

    os.utime(old, (1_700_000_000, 1_700_000_000))
    assert [p.name for p in si.discover_sessions("claude", home=tmp_path)] == [
        "new.jsonl",
        "old.jsonl",
    ]
    assert si.discover_sessions("opencode", home=tmp_path) == []


async def test_import_writes_a_resumable_thread_and_export_round_trips(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sessions.db"
    path = tmp_path / "abc.jsonl"
    _claude_jsonl(path)
    with patch.object(sessions, "get_db_path", return_value=db):
        summary = await si.import_sessions("claude", paths=[path], agent_name="agent")
        assert len(summary.imported) == 1 and summary.skipped == 0
        thread_id, title = summary.imported[0]
        assert title == "Fix the ratchet"
        threads = await sessions.list_threads(include_message_count=True)
        listed = next(t for t in threads if t["thread_id"] == thread_id)
        assert listed["agent_name"] == "agent"
        assert listed.get("message_count") == 3
        meta = await sessions.get_thread_metadata(thread_id)
        assert meta.get("label") == "Fix the ratchet"
        assert "imported" in (meta.get("tags") or [])
        out = await si.export_thread(thread_id, tmp_path / "export.jsonl")
        assert out is not None
        head = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        assert (
            head["format"] == "com.bogware.thread"
            and head["title"] == "Fix the ratchet"
        )
        again = await si.import_sessions("bog", paths=[out])
        assert len(again.imported) == 1
        assert await si.export_thread("missing", tmp_path / "x.jsonl") is None
    text = si.format_import_summary(summary)
    assert "Imported 1 claude session(s)" in text and "/resume" in text


async def test_dry_run_and_unknown_source(tmp_path: Path) -> None:
    path = tmp_path / "abc.jsonl"
    _claude_jsonl(path)
    summary = await si.import_sessions("claude", paths=[path], dry_run=True)
    assert summary.imported[0][0].startswith("(dry-run)")
    assert "unknown source" in (await si.import_sessions("opencode", paths=[])).notes[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["plugin", "import", "claude", "--dry-run"],
        ["plugin", "install", "x.zip", "--sha256", "ab"],
        ["threads", "import", "codex", "--limit", "3"],
        ["threads", "export", "t1", "--out", "o.jsonl"],
    ],
)
def test_cli_parsers(argv: list[str]) -> None:
    import argparse

    from bog_agents_cli.cmd_plugin import (
        setup_plugin_parser,
        setup_threads_transfer_parsers,
    )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    setup_plugin_parser(sub)
    threads = sub.add_parser("threads")
    setup_threads_transfer_parsers(threads.add_subparsers(dest="threads_command"))
    args = parser.parse_args(argv)
    assert args.command == argv[0]
