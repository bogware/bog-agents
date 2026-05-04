"""Tests for the hierarchical AGENTS.md / CLAUDE.md cascade."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.project_utils import find_hierarchical_agent_md


def test_returns_empty_when_no_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    cwd = project / "src" / "feature"
    home.mkdir()
    cwd.mkdir(parents=True)
    paths = find_hierarchical_agent_md(user_cwd=cwd, project_root=project, home=home)
    assert paths == []


def test_home_then_project_then_cwd_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    cwd = project / "src" / "feature"
    cwd.mkdir(parents=True)
    home.mkdir()
    bog_dir = home / ".bog-agents"
    bog_dir.mkdir()
    (bog_dir / "AGENTS.md").write_text("HOME", encoding="utf-8")
    (project / "AGENTS.md").write_text("PROJECT", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("CWD", encoding="utf-8")

    paths = find_hierarchical_agent_md(user_cwd=cwd, project_root=project, home=home)
    contents = [p.read_text(encoding="utf-8") for p in paths]
    assert contents.index("HOME") < contents.index("PROJECT") < contents.index("CWD")


def test_intermediate_dirs_load(tmp_path: Path) -> None:
    """A dir between project_root and cwd contributes its own AGENTS.md."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    mid = project / "src"
    cwd = mid / "feature"
    cwd.mkdir(parents=True)
    home.mkdir()
    (project / "AGENTS.md").write_text("PROJECT", encoding="utf-8")
    (mid / "AGENTS.md").write_text("MID", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("CWD", encoding="utf-8")

    paths = find_hierarchical_agent_md(user_cwd=cwd, project_root=project, home=home)
    rels = [p.read_text(encoding="utf-8") for p in paths]
    assert rels == ["PROJECT", "MID", "CWD"]


def test_dedupes_when_cwd_equals_project_root(tmp_path: Path) -> None:
    """Running from the project root must not list the same file twice."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    home.mkdir()
    (project / "AGENTS.md").write_text("ONCE", encoding="utf-8")

    paths = find_hierarchical_agent_md(
        user_cwd=project, project_root=project, home=home
    )
    assert len(paths) == 1
    assert paths[0].name == "AGENTS.md"


def test_claude_md_also_collected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    cwd = project / "feature"
    cwd.mkdir(parents=True)
    home.mkdir()
    (project / "CLAUDE.md").write_text("PROJECT-CLAUDE", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("CWD-AGENTS", encoding="utf-8")

    names = {
        p.name
        for p in find_hierarchical_agent_md(
            user_cwd=cwd, project_root=project, home=home
        )
    }
    assert names == {"CLAUDE.md", "AGENTS.md"}


def test_handles_missing_project_root(tmp_path: Path) -> None:
    """No project context — fall back to home + cwd only."""
    home = tmp_path / "home"
    cwd = tmp_path / "loose"
    home.mkdir()
    cwd.mkdir()
    (cwd / "AGENTS.md").write_text("CWD", encoding="utf-8")
    paths = find_hierarchical_agent_md(user_cwd=cwd, project_root=None, home=home)
    assert len(paths) == 1
    assert paths[0].name == "AGENTS.md"
