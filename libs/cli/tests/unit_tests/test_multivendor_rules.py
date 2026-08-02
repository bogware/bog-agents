"""Tests for multi-vendor project-rules ingestion (Tier-1 #5)."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.project_utils import (
    _agent_md_in_dir,
    find_hierarchical_agent_md,
    find_project_agent_md,
)


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestAgentMdInDir:
    def test_recognizes_all_top_level_filenames(self, tmp_path: Path) -> None:
        for name in ("AGENTS.md", "AGENT.md", "CLAUDE.md", "CLAUDE.local.md"):
            _write(tmp_path / name)
        names = {p.name for p in _agent_md_in_dir(tmp_path)}
        assert names == {"AGENTS.md", "AGENT.md", "CLAUDE.md", "CLAUDE.local.md"}

    def test_loads_vendor_rules_directories(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude" / "rules" / "style.md")
        _write(tmp_path / ".cursor" / "rules" / "tests.md")
        _write(tmp_path / ".bog-agents" / "rules" / "arch.md")
        names = {p.name for p in _agent_md_in_dir(tmp_path)}
        assert {"style.md", "tests.md", "arch.md"} <= names

    def test_loads_cursor_mdc_rules(self, tmp_path: Path) -> None:
        # Cursor's real project rules are `.mdc`, not `.md`.
        _write(
            tmp_path / ".cursor" / "rules" / "style.mdc", "alwaysApply: true\nuse tabs"
        )
        assert any(p.name == "style.mdc" for p in _agent_md_in_dir(tmp_path))

    def test_loads_cursorrules_single_file(self, tmp_path: Path) -> None:
        _write(tmp_path / ".cursorrules", "use tabs")
        assert any(p.name == ".cursorrules" for p in _agent_md_in_dir(tmp_path))

    def test_non_md_in_rules_dir_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path / ".claude" / "rules" / "notes.txt")
        _write(tmp_path / ".claude" / "rules" / "keep.md")
        names = {p.name for p in _agent_md_in_dir(tmp_path)}
        assert "keep.md" in names
        assert "notes.txt" not in names

    def test_top_level_files_before_rules_dirs(self, tmp_path: Path) -> None:
        _write(tmp_path / "AGENTS.md")
        _write(tmp_path / ".claude" / "rules" / "z.md")
        result = [p.name for p in _agent_md_in_dir(tmp_path)]
        assert result.index("AGENTS.md") < result.index("z.md")


class TestFindProjectAgentMd:
    def test_includes_claude_local_and_rules(self, tmp_path: Path) -> None:
        _write(tmp_path / "AGENTS.md")
        _write(tmp_path / "CLAUDE.local.md")
        _write(tmp_path / ".cursor" / "rules" / "r.md")
        names = {p.name for p in find_project_agent_md(tmp_path)}
        assert {"AGENTS.md", "CLAUDE.local.md", "r.md"} <= names

    def test_dotbog_agents_md_first(self, tmp_path: Path) -> None:
        _write(tmp_path / ".bog-agents" / "AGENTS.md")
        _write(tmp_path / "AGENTS.md")
        paths = find_project_agent_md(tmp_path)
        assert paths[0] == tmp_path / ".bog-agents" / "AGENTS.md"


class TestHierarchicalDeeperWins:
    def test_deeper_rules_appear_after_shallower(self, tmp_path: Path) -> None:
        # project root has a rule; a nested cwd has its own — deeper must be later.
        root = tmp_path / "proj"
        sub = root / "packages" / "web"
        _write(root / ".cursor" / "rules" / "root.md")
        _write(sub / ".cursor" / "rules" / "web.md")
        paths = find_hierarchical_agent_md(
            user_cwd=sub, project_root=root, home=tmp_path / "nohome"
        )
        names = [p.name for p in paths]
        assert "root.md" in names and "web.md" in names
        assert names.index("root.md") < names.index("web.md")
