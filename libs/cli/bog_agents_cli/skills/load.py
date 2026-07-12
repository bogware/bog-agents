"""Skill loader for CLI commands.

This module provides filesystem-based skill discovery for CLI operations
(list, create, info, delete). It wraps the prebuilt middleware functionality from
bog_agents.middleware.skills and adapts it for direct filesystem access
needed by CLI commands.

For middleware usage within agents, use
bog_agents.middleware.skills.SkillsMiddleware directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from bog_agents.backends.filesystem import FilesystemBackend

if TYPE_CHECKING:
    from pathlib import Path
from bog_agents.middleware.skills import (
    SkillMetadata,
    _list_skills as list_skills_from_backend,  # noqa: PLC2701  # Intentional access to internal skill listing
)

from bog_agents_cli._version import __version__ as _cli_version
from bog_agents_cli.skill_trust import install_symlink_trust_hook

logger = logging.getLogger(__name__)

# Teach the SDK skill loader to honor the user's skill-trust store so an
# explicitly-trusted symlinked skill directory is loaded instead of refused.
# Idempotent and fail-closed: with no trust file, every symlink is still
# refused (the default P1-8 posture).
install_symlink_trust_hook()


class ExtendedSkillMetadata(SkillMetadata):
    """Extended skill metadata for CLI display, adds source tracking.

    Attributes:
        source: Origin of the skill. One of `'built-in'`, `'extension'`,
            `'user'`, or `'project'`.
        fs_path: Real filesystem path to the SKILL.md file (not the virtual backend path).
    """

    source: str
    fs_path: str


def _virtual_to_fs_path(root_dir: Path, virtual_path: str) -> str:
    """Convert a virtual backend path to a real filesystem path.

    Args:
        root_dir: The backend's root directory on disk.
        virtual_path: Virtual path as returned by `FilesystemBackend` (e.g. `/web-research/SKILL.md`).

    Returns:
        Absolute filesystem path string.
    """
    return str(root_dir / virtual_path.lstrip("/"))


# Re-export for CLI commands
__all__ = ["SkillMetadata", "list_skills"]


def list_skills(
    *,
    built_in_skills_dir: Path | None = None,
    extension_skills_dirs: list[Path] | None = None,
    user_skills_dir: Path | None = None,
    project_skills_dir: Path | None = None,
    user_agent_skills_dir: Path | None = None,
    project_agent_skills_dir: Path | None = None,
) -> list[ExtendedSkillMetadata]:
    """List skills from built-in, user, and/or project directories.

    This is a CLI-specific wrapper around the prebuilt middleware's skill loading
    functionality. It uses FilesystemBackend to load skills from local directories.

    Precedence order (lowest to highest):
    0. `built_in_skills_dir` (`<package>/built_in_skills/`)
    1. `extension_skills_dirs` (installed extension-provided skills)
    2. `user_skills_dir` (`~/.bog-agents/{agent}/skills/`)
    3. `user_agent_skills_dir` (`~/.agents/skills/`)
    4. `project_skills_dir` (`.bog-agents/skills/`)
    5. `project_agent_skills_dir` (`.agents/skills/`)

    Skills from higher-precedence directories override those with the same name.

    Args:
        built_in_skills_dir: Path to built-in skills shipped with the package.
        extension_skills_dirs: Directories contributed by enabled extensions.
        user_skills_dir: Path to `~/.bog-agents/{agent}/skills/`.
        project_skills_dir: Path to `.bog-agents/skills/`.
        user_agent_skills_dir: Path to `~/.agents/skills/` (alias).
        project_agent_skills_dir: Path to `.agents/skills/` (alias).

    Returns:
        Merged list of skill metadata from all sources, with higher-precedence
            directories taking priority when names conflict.
    """
    all_skills: dict[str, ExtendedSkillMetadata] = {}

    # Load in precedence order (lowest to highest).
    # Each source is wrapped in try/except so that a single inaccessible
    # directory (e.g. permission error) does not prevent skills from other
    # healthy directories from being listed.

    # 0. Built-in skills (<package>/built_in_skills/) - lowest priority
    if built_in_skills_dir and built_in_skills_dir.exists():
        try:
            # virtual_mode=True: paths returned by the backend are virtual
            # (anchored at root_dir). Downstream callers use the `fs_path` field
            # populated below via `_virtual_to_fs_path` for real-path access.
            # Explicit to survive SDK default flips.
            built_in_backend = FilesystemBackend(
                root_dir=str(built_in_skills_dir), virtual_mode=True
            )
            built_in_skills = list_skills_from_backend(
                backend=built_in_backend, source_path="."
            )
            for skill in built_in_skills:
                # Inject the installed CLI version into built-in skill metadata
                # so consumers can see which version shipped the skill.
                enriched_metadata = {
                    **skill["metadata"],
                    "bog-agents-cli-version": _cli_version,
                }
                # cast(): type checkers can't infer TypedDict from spread syntax
                extended_skill = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        "source": "built-in",
                        "metadata": enriched_metadata,
                        "fs_path": _virtual_to_fs_path(
                            built_in_skills_dir, skill["path"]
                        ),
                    },
                )
                all_skills[skill["name"]] = extended_skill
        except OSError:
            logger.warning(
                "Could not load built-in skills from %s",
                built_in_skills_dir,
                exc_info=True,
            )

    # 1. Enabled extension skills
    for extension_dir in extension_skills_dirs or []:
        if not extension_dir.exists():
            continue
        try:
            extension_backend = FilesystemBackend(
                root_dir=str(extension_dir), virtual_mode=True
            )
            extension_skills = list_skills_from_backend(
                backend=extension_backend, source_path="."
            )
            for skill in extension_skills:
                extended_skill = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        "source": "extension",
                        "fs_path": _virtual_to_fs_path(extension_dir, skill["path"]),
                    },
                )
                all_skills[skill["name"]] = extended_skill
        except OSError:
            logger.warning(
                "Could not load extension skills from %s",
                extension_dir,
                exc_info=True,
            )

    # 2. User bog-agents skills (~/.bog-agents/{agent}/skills/)
    if user_skills_dir and user_skills_dir.exists():
        try:
            user_backend = FilesystemBackend(
                root_dir=str(user_skills_dir), virtual_mode=True
            )
            user_skills = list_skills_from_backend(
                backend=user_backend, source_path="."
            )
            for skill in user_skills:
                # cast(): type checkers can't infer TypedDict from spread syntax
                extended_skill = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        "source": "user",
                        "fs_path": _virtual_to_fs_path(user_skills_dir, skill["path"]),
                    },
                )
                all_skills[skill["name"]] = extended_skill
        except OSError:
            logger.warning(
                "Could not load user skills from %s",
                user_skills_dir,
                exc_info=True,
            )

    # 3. User agent skills (~/.agents/skills/) - overrides user bog-agents
    if user_agent_skills_dir and user_agent_skills_dir.exists():
        try:
            user_agent_backend = FilesystemBackend(
                root_dir=str(user_agent_skills_dir), virtual_mode=True
            )
            user_agent_skills = list_skills_from_backend(
                backend=user_agent_backend, source_path="."
            )
            for skill in user_agent_skills:
                # cast(): type checkers can't infer TypedDict from spread syntax
                extended_skill = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        "source": "user",
                        "fs_path": _virtual_to_fs_path(
                            user_agent_skills_dir, skill["path"]
                        ),
                    },
                )
                all_skills[skill["name"]] = extended_skill
        except OSError:
            logger.warning(
                "Could not load user agent skills from %s",
                user_agent_skills_dir,
                exc_info=True,
            )

    # 4. Project bog-agents skills (.bog-agents/skills/)
    if project_skills_dir and project_skills_dir.exists():
        try:
            project_backend = FilesystemBackend(
                root_dir=str(project_skills_dir), virtual_mode=True
            )
            project_skills = list_skills_from_backend(
                backend=project_backend, source_path="."
            )
            for skill in project_skills:
                # cast(): type checkers can't infer TypedDict from spread syntax
                extended_skill = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        "source": "project",
                        "fs_path": _virtual_to_fs_path(
                            project_skills_dir, skill["path"]
                        ),
                    },
                )
                all_skills[skill["name"]] = extended_skill
        except OSError:
            logger.warning(
                "Could not load project skills from %s",
                project_skills_dir,
                exc_info=True,
            )

    # 5. Project agent skills (.agents/skills/) - highest priority
    if project_agent_skills_dir and project_agent_skills_dir.exists():
        try:
            project_agent_backend = FilesystemBackend(
                root_dir=str(project_agent_skills_dir),
                virtual_mode=True,
            )
            project_agent_skills = list_skills_from_backend(
                backend=project_agent_backend, source_path="."
            )
            for skill in project_agent_skills:
                # cast(): type checkers can't infer TypedDict from spread syntax
                extended_skill = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        "source": "project",
                        "fs_path": _virtual_to_fs_path(
                            project_agent_skills_dir, skill["path"]
                        ),
                    },
                )
                all_skills[skill["name"]] = extended_skill
        except OSError:
            logger.warning(
                "Could not load project agent skills from %s",
                project_agent_skills_dir,
                exc_info=True,
            )

    return list(all_skills.values())
