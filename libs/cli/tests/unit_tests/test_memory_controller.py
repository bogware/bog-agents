"""ROADMAP #75: the `/memory` command body."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bog_agents_cli import memory_controller as mc

STORE = """## Agent-Recorded Memories
<!-- bog-agents auto-memories: written by the agent via the `remember` tool. Safe to edit, reorganize, or delete. -->

- (convention) Run tests with make test
- (convention) Run tests with make test
- (gotcha) Windows needs utf-8
"""


class _Msg:
    def __init__(self, kind: str, content: object) -> None:
        self.type = kind
        self.content = content


def test_transcript_text_and_args() -> None:
    messages = [
        _Msg("human", "fix   the bug"),
        _Msg("tool", "ignored"),
        _Msg("ai", [{"type": "text", "text": "done"}]),
        _Msg("ai", ""),
    ]
    assert mc.transcript_text(messages) == "human: fix the bug\nai: done"
    assert mc.transcript_text([_Msg("ai", "x" * 100)], max_chars=20) == "x" * 20
    assert mc.parse_rebuild_args(
        ["--global", "--threads", "3", "--dedup", "prefer", "uv"]
    ) == {"global": True, "threads": 3, "dedup": True, "steer": "prefer uv"}
    assert (
        mc.parse_rebuild_args(["--steer", "keep it short"])["steer"] == "keep it short"
    )
    with pytest.raises(ValueError, match="whole number"):
        mc.parse_rebuild_args(["--threads", "x"])
    with pytest.raises(ValueError, match="between"):
        mc.parse_rebuild_args(["--threads", "999"])


def test_memory_command_flow(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(STORE, encoding="utf-8")
    loads: list[int] = []

    async def _transcripts(limit: int) -> list[tuple[str, str]]:
        loads.append(limit)
        return [("t1", "human: we moved to uv run pytest\nai: noted")]

    def _invoke(prompt: str) -> str:
        assert "thread:t1" in prompt
        return json.dumps(
            {
                "entries": [
                    {
                        "text": "Run tests with uv run pytest",
                        "category": "convention",
                        "sources": ["thread:t1"],
                    },
                    {"text": "Windows needs utf-8", "category": "gotcha"},
                ],
                "notes": ["make test is gone"],
            }
        )

    async def main() -> None:
        assert "No memory rebuild candidate pending" in await mc.run_memory_command(
            "/memory", tmp_path
        )
        assert "No candidate pending" in await mc.run_memory_command(
            "/memory show", tmp_path
        )
        assert "No candidate pending" in await mc.run_memory_command(
            "/memory apply", tmp_path
        )
        out = await mc.run_memory_command(
            "/memory rebuild --threads 2 --steer prefer uv",
            tmp_path,
            invoke=_invoke,
            load_transcripts=_transcripts,
        )
        assert (
            loads == [2]
            and "Memory rebuild (model)" in out
            and "+- (convention) Run tests with uv run pytest" in out
            and "make test is gone" in out
        )
        assert "Candidate pending" in await mc.run_memory_command(
            "/memory status", tmp_path
        )
        assert "uv run pytest" in await mc.run_memory_command("/memory show", tmp_path)
        assert "Applied" in await mc.run_memory_command("/memory apply", tmp_path)
        text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert text.count("uv run pytest") == 1 and "make test" not in text
        # A dedup-only rebuild needs no model and no transcripts.
        out = await mc.run_memory_command(
            "/memory rebuild --dedup --threads 0",
            tmp_path,
            invoke=lambda _p: pytest.fail("model must not be called"),
        )
        assert "Memory rebuild (dedup)" in out and "No changes" in out
        assert "discarded" in await mc.run_memory_command("/memory discard", tmp_path)
        assert "--threads needs" in await mc.run_memory_command(
            "/memory rebuild --threads x", tmp_path
        )
        assert "Unknown verb" in await mc.run_memory_command("/memory dance", tmp_path)

    asyncio.run(main())
