"""Async unit tests for skills middleware with FilesystemBackend.

This module contains async versions of skills middleware tests.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from bog_agents.backends.filesystem import FilesystemBackend
from bog_agents.backends.protocol import FileDownloadResponse
from bog_agents.middleware import skills as skills_module
from bog_agents.middleware.skills import SkillsMiddleware, _alist_skills
from tests.unit_tests.chat_model import GenericFakeChatModel


def make_skill_content(name: str, description: str) -> str:
    """Create SKILL.md content with YAML frontmatter.

    Args:
        name: Skill name for frontmatter
        description: Skill description for frontmatter

    Returns:
        Complete SKILL.md content as string
    """
    return f"""---
name: {name}
description: {description}
---

# {name.title()} Skill

Instructions go here.
"""


async def test_alist_skills_from_backend_single_skill(tmp_path: Path) -> None:
    """Test listing a single skill from filesystem backend (async)."""
    # Create backend with actual filesystem (no virtual mode)
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create skill using backend's upload_files interface
    skills_dir = tmp_path / "skills"
    skill_path = str(skills_dir / "my-skill" / "SKILL.md")
    skill_content = make_skill_content("my-skill", "My test skill")

    responses = backend.upload_files([(skill_path, skill_content.encode("utf-8"))])
    assert responses[0].error is None

    # List skills using the full absolute path
    skills = await _alist_skills(backend, str(skills_dir))

    assert skills == [
        {
            "name": "my-skill",
            "description": "My test skill",
            "path": skill_path,
            "metadata": {},
            "license": None,
            "compatibility": None,
            "allowed_tools": [],
        }
    ]


async def test_alist_skills_from_backend_multiple_skills(tmp_path: Path) -> None:
    """Test listing multiple skills from filesystem backend (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create multiple skills using backend's upload_files interface
    skills_dir = tmp_path / "skills"
    skill1_path = str(skills_dir / "skill-one" / "SKILL.md")
    skill2_path = str(skills_dir / "skill-two" / "SKILL.md")
    skill3_path = str(skills_dir / "skill-three" / "SKILL.md")

    skill1_content = make_skill_content("skill-one", "First skill")
    skill2_content = make_skill_content("skill-two", "Second skill")
    skill3_content = make_skill_content("skill-three", "Third skill")

    responses = backend.upload_files(
        [
            (skill1_path, skill1_content.encode("utf-8")),
            (skill2_path, skill2_content.encode("utf-8")),
            (skill3_path, skill3_content.encode("utf-8")),
        ]
    )

    assert all(r.error is None for r in responses)

    # List skills
    skills = await _alist_skills(backend, str(skills_dir))

    # Should return all three skills (order may vary)
    assert len(skills) == 3
    skill_names = {s["name"] for s in skills}
    assert skill_names == {"skill-one", "skill-two", "skill-three"}


async def test_alist_skills_from_backend_empty_directory(tmp_path: Path) -> None:
    """Test listing skills from an empty directory (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create empty skills directory
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Should return empty list
    skills = await _alist_skills(backend, str(skills_dir))
    assert skills == []


async def test_alist_skills_from_backend_nonexistent_path(tmp_path: Path) -> None:
    """Test listing skills from a path that doesn't exist (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Try to list from non-existent directory
    skills = await _alist_skills(backend, str(tmp_path / "nonexistent"))
    assert skills == []


async def test_alist_skills_from_backend_missing_skill_md(tmp_path: Path) -> None:
    """Test that directories without SKILL.md are skipped (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create a valid skill and an invalid one (missing SKILL.md)
    skills_dir = tmp_path / "skills"
    valid_skill_path = str(skills_dir / "valid-skill" / "SKILL.md")
    invalid_dir_file = str(skills_dir / "invalid-skill" / "readme.txt")

    valid_content = make_skill_content("valid-skill", "Valid skill")

    backend.upload_files(
        [
            (valid_skill_path, valid_content.encode("utf-8")),
            (invalid_dir_file, b"Not a skill file"),
        ]
    )

    # List skills - should only get the valid one
    skills = await _alist_skills(backend, str(skills_dir))

    assert skills == [
        {
            "name": "valid-skill",
            "description": "Valid skill",
            "path": valid_skill_path,
            "metadata": {},
            "license": None,
            "compatibility": None,
            "allowed_tools": [],
        }
    ]


async def test_alist_skills_from_backend_invalid_frontmatter(tmp_path: Path) -> None:
    """Test that skills with invalid YAML frontmatter are skipped (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    skills_dir = tmp_path / "skills"
    valid_skill_path = str(skills_dir / "valid-skill" / "SKILL.md")
    invalid_skill_path = str(skills_dir / "invalid-skill" / "SKILL.md")

    valid_content = make_skill_content("valid-skill", "Valid skill")
    invalid_content = """---
name: invalid-skill
description: [unclosed yaml
---

Content
"""

    backend.upload_files(
        [
            (valid_skill_path, valid_content.encode("utf-8")),
            (invalid_skill_path, invalid_content.encode("utf-8")),
        ]
    )

    # Should only get the valid skill
    skills = await _alist_skills(backend, str(skills_dir))

    assert skills == [
        {
            "name": "valid-skill",
            "description": "Valid skill",
            "path": valid_skill_path,
            "metadata": {},
            "license": None,
            "compatibility": None,
            "allowed_tools": [],
        }
    ]


async def test_abefore_agent_loads_skills(tmp_path: Path) -> None:
    """Test that abefore_agent loads skills from backend."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create some skills
    skills_dir = tmp_path / "skills" / "user"
    skill1_path = str(skills_dir / "skill-one" / "SKILL.md")
    skill2_path = str(skills_dir / "skill-two" / "SKILL.md")

    skill1_content = make_skill_content("skill-one", "First skill")
    skill2_content = make_skill_content("skill-two", "Second skill")

    backend.upload_files(
        [
            (skill1_path, skill1_content.encode("utf-8")),
            (skill2_path, skill2_content.encode("utf-8")),
        ]
    )

    sources = [str(skills_dir)]
    middleware = SkillsMiddleware(
        backend=backend,
        sources=sources,
    )

    # Call abefore_agent
    result = await middleware.abefore_agent({}, None, {})  # type: ignore[arg-type]

    assert result is not None
    assert "skills_metadata" in result
    assert len(result["skills_metadata"]) == 2

    skill_names = {s["name"] for s in result["skills_metadata"]}
    assert skill_names == {"skill-one", "skill-two"}


async def test_abefore_agent_skill_override(tmp_path: Path) -> None:
    """Test that skills from later sources override earlier ones (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create same skill name in two sources
    base_dir = tmp_path / "skills" / "base"
    user_dir = tmp_path / "skills" / "user"

    base_skill_path = str(base_dir / "shared-skill" / "SKILL.md")
    user_skill_path = str(user_dir / "shared-skill" / "SKILL.md")

    base_content = make_skill_content("shared-skill", "Base description")
    user_content = make_skill_content("shared-skill", "User description")

    backend.upload_files(
        [
            (base_skill_path, base_content.encode("utf-8")),
            (user_skill_path, user_content.encode("utf-8")),
        ]
    )

    sources = [
        str(base_dir),
        str(user_dir),
    ]
    middleware = SkillsMiddleware(
        backend=backend,
        sources=sources,
    )

    # Call abefore_agent
    result = await middleware.abefore_agent({}, None, {})  # type: ignore[arg-type]

    assert result is not None
    assert len(result["skills_metadata"]) == 1

    # Should have the user version (later source wins)
    skill = result["skills_metadata"][0]
    assert skill == {
        "name": "shared-skill",
        "description": "User description",
        "path": user_skill_path,
        "metadata": {},
        "license": None,
        "compatibility": None,
        "allowed_tools": [],
    }


async def test_abefore_agent_empty_sources(tmp_path: Path) -> None:
    """Test abefore_agent with empty sources (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create empty directories
    (tmp_path / "skills" / "user").mkdir(parents=True)

    sources = [str(tmp_path / "skills" / "user")]
    middleware = SkillsMiddleware(
        backend=backend,
        sources=sources,
    )

    result = await middleware.abefore_agent({}, None, {})  # type: ignore[arg-type]

    assert result is not None
    assert result["skills_metadata"] == []


async def test_abefore_agent_skips_loading_if_metadata_present(tmp_path: Path) -> None:
    """Test that abefore_agent skips loading if skills_metadata is already in state."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create a skill in the backend
    skills_dir = tmp_path / "skills" / "user"
    skill_path = str(skills_dir / "test-skill" / "SKILL.md")
    skill_content = make_skill_content("test-skill", "A test skill")

    backend.upload_files([(skill_path, skill_content.encode("utf-8"))])

    sources = [str(skills_dir)]
    middleware = SkillsMiddleware(
        backend=backend,
        sources=sources,
    )

    # State has skills_metadata already
    state_with_metadata = {"skills_metadata": []}
    result = await middleware.abefore_agent(state_with_metadata, None, {})  # type: ignore[arg-type]

    # Should return None, not load new skills
    assert result is None


async def test_agent_with_skills_middleware_multiple_sources_async(tmp_path: Path) -> None:
    """Test agent with skills from multiple sources (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create skills in multiple sources
    base_dir = tmp_path / "skills" / "base"
    user_dir = tmp_path / "skills" / "user"

    base_skill_path = str(base_dir / "base-skill" / "SKILL.md")
    user_skill_path = str(user_dir / "user-skill" / "SKILL.md")

    base_content = make_skill_content("base-skill", "Base skill description")
    user_content = make_skill_content("user-skill", "User skill description")

    responses = backend.upload_files(
        [
            (base_skill_path, base_content.encode("utf-8")),
            (user_skill_path, user_content.encode("utf-8")),
        ]
    )
    assert all(r.error is None for r in responses)

    # Create fake model
    fake_model = GenericFakeChatModel(messages=iter([AIMessage(content="I see both skills.")]))

    # Create middleware with multiple sources
    sources = [
        str(base_dir),
        str(user_dir),
    ]
    middleware = SkillsMiddleware(
        backend=backend,
        sources=sources,
    )

    # Create agent
    agent = create_agent(model=fake_model, middleware=[middleware])

    # Invoke asynchronously
    result = await agent.ainvoke({"messages": [HumanMessage(content="Help me")]})

    assert "messages" in result
    assert len(result["messages"]) > 0

    # Verify both skills are in system prompt
    first_call = fake_model.call_history[0]
    system_message = first_call["messages"][0]
    content = system_message.text

    assert "base-skill" in content
    assert "user-skill" in content


async def test_agent_with_skills_middleware_empty_sources_async(tmp_path: Path) -> None:
    """Test that agent works with empty skills sources (async)."""
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)

    # Create empty skills directory
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Create fake model
    fake_model = GenericFakeChatModel(messages=iter([AIMessage(content="Working without skills.")]))

    # Create middleware with empty directory
    middleware = SkillsMiddleware(backend=backend, sources=[str(skills_dir)])

    # Create agent
    agent = create_agent(model=fake_model, middleware=[middleware])

    # Invoke asynchronously
    result = await agent.ainvoke({"messages": [HumanMessage(content="Hello")]})

    assert "messages" in result
    assert len(result["messages"]) > 0

    # Verify system prompt still contains Skills System section
    first_call = fake_model.call_history[0]
    system_message = first_call["messages"][0]
    content = system_message.text

    assert "Skills System" in content
    assert "No skills available" in content


# ---------------------------------------------------------------------------
# P1-8 (async): symlinked skill directories must be refused on the async path
#
# The containment guard originally lived only in `_list_skills`. `_alist_skills`
# -- the path `ainvoke` actually takes -- had no check at all, so running the
# agent asynchronously bypassed the guard entirely. Both paths now share
# `_filter_skill_dirs`.
# ---------------------------------------------------------------------------


@dataclass
class _AsyncStubBackend:
    """Minimal async backend recording which SKILL.md paths were requested.

    `adownload_files` returns one `file_not_found` response per requested path so
    the production `zip(..., strict=True)` invariant holds. The assertion is only
    about which paths reached the download step -- a symlinked directory must be
    filtered out before then.
    """

    items_response: list
    downloaded_paths: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.downloaded_paths = []

    async def als_info(self, path: str) -> list:
        return self.items_response

    async def adownload_files(self, paths: list) -> list:
        self.downloaded_paths.extend(paths)
        return [FileDownloadResponse(path=p, content=None, error="file_not_found") for p in paths]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows symlink creation requires admin / dev-mode",
)
async def test_symlinked_skill_dir_is_skipped_async(tmp_path: Path) -> None:
    """A symlinked skill dir is refused via the ASYNC listing path.

    Regression guard: this asserts the fix for the async symlink bypass. Against
    the pre-fix code `_alist_skills` had no containment check and the hostile
    link's SKILL.md was happily downloaded.
    """
    real = tmp_path / "real-skill"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: real\n---\n", encoding="utf-8")
    outside = tmp_path.parent / "hostile-async"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "hostile-skill"
    link.symlink_to(outside)

    backend = _AsyncStubBackend(
        items_response=[
            {"path": str(real), "is_dir": True},
            {"path": str(link), "is_dir": True},
        ]
    )

    await _alist_skills(backend, str(tmp_path))  # type: ignore[arg-type]

    downloaded = backend.downloaded_paths
    assert any("real-skill" in p for p in downloaded)
    assert not any("hostile-skill" in p for p in downloaded), downloaded


async def test_symlinked_skill_dir_is_skipped_async_islink_patched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform-independent twin of the symlink test.

    Real symlink creation needs privileges Windows CI/dev boxes lack, so the
    test above skips there -- which would leave the async containment guard
    completely unexercised on Windows. Patching `os.path.islink` drives the exact
    same branch of `_filter_skill_dirs` on every platform.
    """
    real = tmp_path / "real-skill"
    hostile = tmp_path / "hostile-skill"

    monkeypatch.setattr(
        skills_module.os.path,
        "islink",
        lambda p: str(p) == str(hostile),
    )

    backend = _AsyncStubBackend(
        items_response=[
            {"path": str(real), "is_dir": True},
            {"path": str(hostile), "is_dir": True},
        ]
    )

    await _alist_skills(backend, str(tmp_path))  # type: ignore[arg-type]

    downloaded = backend.downloaded_paths
    assert any("real-skill" in p for p in downloaded)
    assert not any("hostile-skill" in p for p in downloaded), downloaded


async def test_symlinked_skill_dir_refused_through_abefore_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the guard holds through `abefore_agent`, not just the helper."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    good = skills_dir / "good-skill"
    good.mkdir()
    (good / "SKILL.md").write_text(
        make_skill_content("good-skill", "A legitimate skill."),
        encoding="utf-8",
    )
    hostile = skills_dir / "hostile-skill"
    hostile.mkdir()
    (hostile / "SKILL.md").write_text(
        make_skill_content("hostile-skill", "Loaded through a symlink."),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        skills_module.os.path,
        "islink",
        lambda p: Path(p).name == "hostile-skill",
    )

    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=False)
    middleware = SkillsMiddleware(backend=backend, sources=[str(skills_dir)])

    result = await middleware.abefore_agent({"messages": []}, None, {})  # type: ignore[arg-type]

    assert result is not None
    names = {s["name"] for s in result["skills_metadata"]}
    assert names == {"good-skill"}, f"symlinked skill leaked through the async path: {names}"
