"""Tests for agent-written auto-memories (ROADMAP #13)."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.auto_memory import (
    _SECTION,
    append_memory,
    auto_memory_tools,
)


class TestAppendMemory:
    def test_creates_file_and_section(self, tmp_path: Path) -> None:
        p = tmp_path / "AGENTS.md"
        assert (
            append_memory(p, "Tests live under tests/unit_tests", "convention") is True
        )
        text = p.read_text(encoding="utf-8")
        assert _SECTION in text
        assert "auto-memories" in text  # provenance marker
        assert "(convention) Tests live under tests/unit_tests" in text

    def test_dedup_no_duplicate(self, tmp_path: Path) -> None:
        p = tmp_path / "AGENTS.md"
        assert append_memory(p, "Use uv for deps", "note") is True
        assert append_memory(p, "Use uv for deps", "note") is False  # already there
        assert p.read_text(encoding="utf-8").count("Use uv for deps") == 1

    def test_appends_into_existing_section(self, tmp_path: Path) -> None:
        p = tmp_path / "AGENTS.md"
        append_memory(p, "first fact", "note")
        append_memory(p, "second fact", "gotcha")
        text = p.read_text(encoding="utf-8")
        assert "(note) first fact" in text
        assert "(gotcha) second fact" in text
        assert text.count(_SECTION) == 1  # one managed section

    def test_preserves_existing_content_and_following_headings(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "AGENTS.md"
        p.write_text(
            "# Project\n\nSome user notes.\n\n## Other Section\nkeep me\n",
            encoding="utf-8",
        )
        append_memory(p, "remembered thing", "decision")
        text = p.read_text(encoding="utf-8")
        assert "Some user notes." in text
        assert "## Other Section" in text
        assert "keep me" in text
        assert "(decision) remembered thing" in text

    def test_collapses_multiline_fact(self, tmp_path: Path) -> None:
        p = tmp_path / "AGENTS.md"
        append_memory(p, "line one\n   line two", "note")
        assert "(note) line one line two" in p.read_text(encoding="utf-8")


class TestRememberTool:
    def test_remember_writes_project_agents_md(self, tmp_path: Path) -> None:
        (remember,) = auto_memory_tools(working_dir=tmp_path)
        out = remember.invoke(
            {"fact": "Prefer pathlib over os.path", "category": "convention"}
        )
        assert "Recorded" in out
        assert (tmp_path / "AGENTS.md").exists()
        assert "Prefer pathlib over os.path" in (tmp_path / "AGENTS.md").read_text(
            encoding="utf-8"
        )

    def test_remember_empty_fact_is_noop(self, tmp_path: Path) -> None:
        (remember,) = auto_memory_tools(working_dir=tmp_path)
        out = remember.invoke({"fact": "   "})
        assert "Nothing recorded" in out
        assert not (tmp_path / "AGENTS.md").exists()

    def test_remember_dedup_message(self, tmp_path: Path) -> None:
        (remember,) = auto_memory_tools(working_dir=tmp_path)
        remember.invoke({"fact": "X is true", "category": "note"})
        out = remember.invoke({"fact": "X is true", "category": "note"})
        assert "Already remembered" in out

    def test_invalid_scope_defaults_to_project(self, tmp_path: Path) -> None:
        (remember,) = auto_memory_tools(working_dir=tmp_path)
        out = remember.invoke({"fact": "scoped fact", "scope": "bogus"})
        assert "this project" in out
        assert (tmp_path / "AGENTS.md").exists()
