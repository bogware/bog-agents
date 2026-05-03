"""Tests for ``bog_agents_cli.personas``."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli.personas import (
    _parse_frontmatter,
    discover_personas,
    get_persona,
)


def test_parse_frontmatter_extracts_fields() -> None:
    text = "---\nname: terse\ndescription: short answers\n---\nbody here\n"
    front, body = _parse_frontmatter(text)
    assert front == {"name": "terse", "description": "short answers"}
    assert body.strip() == "body here"


def test_parse_frontmatter_ignores_missing_fence() -> None:
    front, body = _parse_frontmatter("plain markdown body")
    assert front == {}
    assert body == "plain markdown body"


def test_parse_frontmatter_strips_quotes() -> None:
    front, _ = _parse_frontmatter('---\nname: "with quotes"\n---\nbody\n')
    assert front["name"] == "with quotes"


def test_discover_returns_empty_when_no_dirs(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    assert discover_personas(project_root=project, home=home) == {}


def test_discover_loads_user_persona(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pers_dir = home / ".bog-agents" / "personas"
    pers_dir.mkdir(parents=True)
    (pers_dir / "terse.md").write_text(
        "---\nname: terse\ndescription: short and direct\n---\nBe terse.\n",
        encoding="utf-8",
    )
    found = discover_personas(project_root=None, home=home)
    assert "terse" in found
    assert found["terse"].description == "short and direct"
    assert "Be terse." in found["terse"].body


def test_project_persona_overrides_user_with_same_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "proj"
    user_dir = home / ".bog-agents" / "personas"
    project_dir = project / ".bog-agents" / "personas"
    user_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (user_dir / "voice.md").write_text(
        "---\nname: voice\ndescription: USER\n---\nuser body\n", encoding="utf-8"
    )
    (project_dir / "voice.md").write_text(
        "---\nname: voice\ndescription: PROJECT\n---\nproject body\n",
        encoding="utf-8",
    )
    found = discover_personas(project_root=project, home=home)
    assert found["voice"].description == "PROJECT"
    assert "project body" in found["voice"].body


def test_persona_id_falls_back_to_filename(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pers_dir = home / ".bog-agents" / "personas"
    pers_dir.mkdir(parents=True)
    (pers_dir / "no-frontmatter.md").write_text("just a body", encoding="utf-8")
    found = discover_personas(project_root=None, home=home)
    assert "no-frontmatter" in found
    assert found["no-frontmatter"].body == "just a body"


def test_get_persona_case_insensitive(tmp_path: Path) -> None:
    home = tmp_path / "home"
    pers_dir = home / ".bog-agents" / "personas"
    pers_dir.mkdir(parents=True)
    (pers_dir / "Terse.md").write_text(
        "---\nname: Terse\n---\nbody\n", encoding="utf-8"
    )
    persona = get_persona("TERSE", project_root=None, home=home)
    assert persona is not None
    assert persona.id == "terse"


def test_persona_command_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/persona" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/persona"] == "_handle_persona_command"
