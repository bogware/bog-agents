"""Tests for the self-improving skill flywheel."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bog_agents_cli.skill_flywheel import (
    SkillProposal,
    accept_proposal,
    list_proposals,
    propose_skills_from_transcript,
    reject_proposal,
    write_proposal,
)


def _proposal(pid: str = "use-conventional-commits") -> SkillProposal:
    return SkillProposal(
        id=pid,
        description="Always commit with conventional-commit prefixes",
        trigger="when running git commit",
        instructions=(
            "Use a Conventional Commits prefix:\n"
            "- feat for new behavior\n"
            "- fix for bug fixes\n"
            "- docs for docs only\n"
        ),
    )


def test_to_skill_md_includes_frontmatter() -> None:
    body = _proposal().to_skill_md()
    assert body.startswith("---\n")
    assert "name: use-conventional-commits" in body
    assert "## Instructions" in body


def test_write_proposal_creates_file(tmp_path: Path) -> None:
    target = write_proposal(_proposal(), skills_dir=tmp_path)
    assert target.exists()
    assert target.parent.name == "proposed"


def test_write_proposal_refuses_overwrite(tmp_path: Path) -> None:
    write_proposal(_proposal(), skills_dir=tmp_path)
    with pytest.raises(FileExistsError):
        write_proposal(_proposal(), skills_dir=tmp_path)


def test_write_proposal_overwrite_succeeds(tmp_path: Path) -> None:
    write_proposal(_proposal(), skills_dir=tmp_path)
    target = write_proposal(_proposal(), skills_dir=tmp_path, overwrite=True)
    assert target.exists()


def test_write_rejects_unsafe_id(tmp_path: Path) -> None:
    bad = SkillProposal(id="../escape", description="x", trigger="x", instructions="x")
    with pytest.raises(ValueError, match="invalid proposal id"):
        write_proposal(bad, skills_dir=tmp_path)


def test_accept_promotes_to_skills_root(tmp_path: Path) -> None:
    write_proposal(_proposal(), skills_dir=tmp_path)
    dest = accept_proposal("use-conventional-commits", skills_dir=tmp_path)
    assert dest.exists()
    assert dest.parent == tmp_path
    # Original under proposed/ should be gone.
    assert not (tmp_path / "proposed" / "use-conventional-commits.md").exists()


def test_accept_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        accept_proposal("nope", skills_dir=tmp_path)


def test_reject_removes_proposal(tmp_path: Path) -> None:
    write_proposal(_proposal(), skills_dir=tmp_path)
    assert reject_proposal("use-conventional-commits", skills_dir=tmp_path)
    assert list_proposals(skills_dir=tmp_path) == []


def test_reject_unknown_returns_false(tmp_path: Path) -> None:
    assert reject_proposal("nope", skills_dir=tmp_path) is False


async def test_propose_parses_well_formed_json() -> None:
    raw = (
        '[{"id":"use-conventional-commits","description":"...",'
        '"trigger":"when committing","instructions":"prefix with feat/fix/etc"}]'
    )
    response = MagicMock()
    response.content = raw
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)

    proposals = await propose_skills_from_transcript(
        "user: how do I commit?\nagent: like this", model
    )
    assert len(proposals) == 1
    assert proposals[0].id == "use-conventional-commits"


async def test_propose_extracts_json_from_chatty_response() -> None:
    raw = (
        "Here are my proposals:\n"
        '[{"id":"strict-asserts","description":"x","trigger":"y","instructions":"z"}]\n'
        "Hope this helps."
    )
    response = MagicMock()
    response.content = raw
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)
    proposals = await propose_skills_from_transcript("transcript", model)
    assert len(proposals) == 1
    assert proposals[0].id == "strict-asserts"


async def test_propose_returns_empty_on_invalid_json() -> None:
    response = MagicMock()
    response.content = "this is not JSON"
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)
    assert await propose_skills_from_transcript("x", model) == []


async def test_propose_caps_at_five() -> None:
    items = [
        f'{{"id":"sk{i}","description":"d{i}","trigger":"t{i}","instructions":"i{i}"}}'
        for i in range(10)
    ]
    raw = "[" + ", ".join(items) + "]"
    response = MagicMock()
    response.content = raw
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=response)
    proposals = await propose_skills_from_transcript("x", model)
    assert len(proposals) == 5


def test_teach_command_registered() -> None:
    from bog_agents_cli.commands import COMMAND_HANDLER_MAP

    assert "/teach" in COMMAND_HANDLER_MAP
    assert COMMAND_HANDLER_MAP["/teach"] == "_handle_teach_command"
