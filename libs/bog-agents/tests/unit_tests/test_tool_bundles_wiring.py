"""Tests for the wiring-pass tool bundles (background shell + memory search)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from bog_agents.backends.local_shell import LocalShellBackend
from bog_agents.tools import background_shell_tools_bundle, memory_search_tool_bundle

PY = sys.executable


class TestBackgroundShellToolsBundle:
    def test_returns_four_tools(self) -> None:
        be = LocalShellBackend(root_dir=tempfile.mkdtemp(), inherit_env=True)
        try:
            names = {t.name for t in background_shell_tools_bundle(be)}
            assert names == {
                "poll_background_command",
                "wait_background_command",
                "kill_background_command",
                "list_background_commands",
            }
        finally:
            be.close()

    def test_empty_for_backend_without_api(self) -> None:
        class _Plain:
            pass

        assert background_shell_tools_bundle(_Plain()) == []

    def test_tools_operate_on_a_real_background_command(self) -> None:
        be = LocalShellBackend(root_dir=tempfile.mkdtemp(), inherit_env=True)
        try:
            tools = {t.name: t for t in background_shell_tools_bundle(be)}
            resp = be.execute(f'{PY} -c "import time; time.sleep(20)"', background=True)
            tid = resp.output.split("task ", 1)[1].split(".", 1)[0].strip()
            # Call the underlying func directly (ToolRuntime is injected by the
            # agent at run time; the closures `del runtime` so None is fine).
            listed = tools["list_background_commands"].func(None)
            assert tid in listed
            killed = tools["kill_background_command"].func(None, tid)
            assert "Killed" in killed
        finally:
            be.close()


class TestMemorySearchToolBundle:
    def test_searches_memory_files(self, tmp_path: Path) -> None:
        a = tmp_path / "AGENTS.md"
        a.write_text("Use httpx not requests.\n\nConfig lives in ~/.bog-agents.", encoding="utf-8")
        b = tmp_path / "CLAUDE.md"
        b.write_text("Run the migration with --dry-run first.", encoding="utf-8")
        tools = memory_search_tool_bundle([a, b])
        assert [t.name for t in tools] == ["memory_search"]
        out = tools[0].func(None, "migration")
        assert "dry-run" in out
        assert "CLAUDE.md" in out  # names the source file

    def test_no_match_message(self, tmp_path: Path) -> None:
        a = tmp_path / "AGENTS.md"
        a.write_text("Use httpx not requests.", encoding="utf-8")
        out = memory_search_tool_bundle([a])[0].func(None, "kubernetes")
        assert "No memory matched" in out

    def test_empty_when_no_readable_sources(self, tmp_path: Path) -> None:
        assert memory_search_tool_bundle([tmp_path / "missing.md"]) == []
