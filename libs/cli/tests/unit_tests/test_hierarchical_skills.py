"""Tests for the hierarchical ``.bog-agents/skills`` discovery."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.project_utils import find_hierarchical_skill_dirs


def _make_skills_dir(directory: Path) -> Path:
    target = directory / ".bog-agents" / "skills"
    target.mkdir(parents=True)
    return target


def test_returns_empty_when_no_dirs(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    cwd = project / "src"
    cwd.mkdir(parents=True)
    assert find_hierarchical_skill_dirs(user_cwd=cwd, project_root=project) == []


def test_project_only(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    cwd = project / "src"
    cwd.mkdir(parents=True)
    proj_skills = _make_skills_dir(project)
    paths = find_hierarchical_skill_dirs(user_cwd=cwd, project_root=project)
    assert paths == [proj_skills]


def test_project_then_intermediate_then_cwd(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    mid = project / "src"
    cwd = mid / "feature"
    cwd.mkdir(parents=True)
    proj = _make_skills_dir(project)
    mid_skills = _make_skills_dir(mid)
    cwd_skills = _make_skills_dir(cwd)
    paths = find_hierarchical_skill_dirs(user_cwd=cwd, project_root=project)
    assert paths == [proj, mid_skills, cwd_skills]


def test_dedupes_when_cwd_equals_project_root(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    proj_skills = _make_skills_dir(project)
    paths = find_hierarchical_skill_dirs(user_cwd=project, project_root=project)
    assert paths == [proj_skills]


def test_no_project_falls_back_to_cwd_only(tmp_path: Path) -> None:
    cwd = tmp_path / "loose"
    cwd.mkdir()
    skills = _make_skills_dir(cwd)
    paths = find_hierarchical_skill_dirs(user_cwd=cwd, project_root=None)
    assert paths == [skills]


def test_cwd_outside_project_root_uses_both(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    proj_skills = _make_skills_dir(project)
    elsewhere_skills = _make_skills_dir(elsewhere)
    paths = find_hierarchical_skill_dirs(user_cwd=elsewhere, project_root=project)
    assert proj_skills in paths
    assert elsewhere_skills in paths
