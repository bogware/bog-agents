"""Utilities for project root detection and project-specific configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli._server_constants import ENV_PREFIX as _ENV_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectContext:
    """Explicit user/project path context for project-sensitive behavior.

    Attributes:
        user_cwd: Authoritative working directory from the CLI invocation.
        project_root: Resolved project root for `user_cwd`, if one exists.
    """

    user_cwd: Path
    project_root: Path | None = None

    def __post_init__(self) -> None:
        """Validate that path fields are absolute.

        Raises:
            ValueError: If `user_cwd` or `project_root` is not absolute.
        """
        if not self.user_cwd.is_absolute():
            msg = f"user_cwd must be absolute, got {self.user_cwd!r}"
            raise ValueError(msg)
        if self.project_root is not None and not self.project_root.is_absolute():
            msg = f"project_root must be absolute, got {self.project_root!r}"
            raise ValueError(msg)

    @classmethod
    def from_user_cwd(cls, user_cwd: str | Path) -> ProjectContext:
        """Build a project context from an explicit user working directory.

        Args:
            user_cwd: User invocation directory.

        Returns:
            Resolved project context.
        """
        resolved_cwd = Path(user_cwd).expanduser().resolve()
        return cls(
            user_cwd=resolved_cwd,
            project_root=find_project_root(resolved_cwd),
        )

    def resolve_user_path(self, path: str | Path) -> Path:
        """Resolve a path relative to the explicit user working directory.

        Args:
            path: Absolute or relative user-facing path.

        Returns:
            Absolute resolved path.
        """
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.user_cwd / candidate).resolve()

    def project_agent_md_paths(self) -> list[Path]:
        """Return project-level `AGENTS.md` files for this context."""
        if self.project_root is None:
            return []
        return find_project_agent_md(self.project_root)

    def hierarchical_agent_md_paths(self) -> list[Path]:
        """Return AGENTS.md / CLAUDE.md files in cascading precedence.

        Walks home → project root → ancestor dirs → cwd, deduping.
        See :func:`find_hierarchical_agent_md` for the full ordering.
        """
        return find_hierarchical_agent_md(
            user_cwd=self.user_cwd,
            project_root=self.project_root,
        )

    def hierarchical_skill_dirs(self) -> list[Path]:
        """Return ``.bog-agents/skills/`` dirs in shallow→deep precedence.

        Walk from the project root down to the user's cwd (each ancestor
        contributes if it has a ``.bog-agents/skills`` subdir). The
        deepest dir loads last, so a feature-branch's local override
        beats a project-wide skill of the same name.
        """
        return find_hierarchical_skill_dirs(
            user_cwd=self.user_cwd,
            project_root=self.project_root,
        )

    def project_skills_dir(self) -> Path | None:
        """Return the project `.bog-agents/skills` directory, if any."""
        if self.project_root is None:
            return None
        return self.project_root / ".bog-agents" / "skills"

    def project_agents_dir(self) -> Path | None:
        """Return the project `.bog-agents/agents` directory, if any."""
        if self.project_root is None:
            return None
        return self.project_root / ".bog-agents" / "agents"

    def project_agent_skills_dir(self) -> Path | None:
        """Return the project `.agents/skills` directory, if any."""
        if self.project_root is None:
            return None
        return self.project_root / ".agents" / "skills"


def get_server_project_context(
    env: Mapping[str, str] | None = None,
) -> ProjectContext | None:
    """Read the server project context from environment transport data.

    Args:
        env: Environment mapping to read from.

    Returns:
        Reconstructed project context, or `None` if no server context exists.
    """
    environment = os.environ if env is None else env
    raw_cwd = environment.get(f"{_ENV_PREFIX}CWD")
    if not raw_cwd:
        return None

    try:
        user_cwd = Path(raw_cwd).expanduser().resolve()
        raw_project_root = environment.get(f"{_ENV_PREFIX}PROJECT_ROOT")
        project_root = (
            Path(raw_project_root).expanduser().resolve()
            if raw_project_root
            else find_project_root(user_cwd)
        )
    except OSError:
        logger.warning(
            "Could not resolve server project context from CWD=%s",
            raw_cwd,
            exc_info=True,
        )
        return None

    return ProjectContext(user_cwd=user_cwd, project_root=project_root)


def find_project_root(start_path: str | Path | None = None) -> Path | None:
    """Find the project root by looking for .git directory.

    Walks up the directory tree from start_path (or cwd) looking for a .git
    directory, which indicates the project root.

    Args:
        start_path: Directory to start searching from.
            Defaults to current working directory.

    Returns:
        Path to the project root if found, None otherwise.
    """
    current = Path(start_path or Path.cwd()).expanduser().resolve()

    # Walk up the directory tree
    for parent in [current, *list(current.parents)]:
        git_dir = parent / ".git"
        if git_dir.exists():
            return parent

    return None


def find_project_agent_md(project_root: Path) -> list[Path]:
    """Find project-specific instruction files (multi-vendor; Tier-1 #5).

    Returns ALL that exist, in load order:
    1. ``project_root/.bog-agents/AGENTS.md``
    2. Top-level files: ``AGENTS.md``, ``AGENT.md``, ``CLAUDE.md``,
       ``CLAUDE.local.md``
    3. Every ``*.md`` under ``.bog-agents/rules/``, ``.claude/rules/``,
       ``.cursor/rules/``, plus ``.cursorrules``

    Loading Claude Code (``CLAUDE.md``/``CLAUDE.local.md``) and Cursor
    (``.cursor/rules/``/``.cursorrules``) conventions alongside AGENTS.md lets a
    repo carry all three ecosystems' instructions at once.

    Args:
        project_root: Path to the project root directory.

    Returns:
        Existing context file paths in load order (empty if none exist).
    """
    paths: list[Path] = []
    dotbog = project_root / ".bog-agents" / "AGENTS.md"
    try:
        if dotbog.is_file():
            paths.append(dotbog)
    except OSError:
        pass
    # Top-level instruction files (AGENTS/AGENT/CLAUDE/CLAUDE.local) + the
    # vendor-neutral rules directories (.bog-agents/.claude/.cursor rules).
    paths.extend(_agent_md_in_dir(project_root))
    return paths


def find_hierarchical_skill_dirs(
    *,
    user_cwd: Path,
    project_root: Path | None,
) -> list[Path]:
    """Return ``.bog-agents/skills/`` dirs from project root → cwd.

    Each ancestor directory between ``project_root`` (inclusive) and
    ``user_cwd`` (inclusive) is checked; only existing dirs are
    returned. Order is shallowest → deepest so the deepest dir wins
    when SkillsMiddleware encounters duplicate skill names.

    Args:
        user_cwd: User's invocation directory.
        project_root: Detected project root, or ``None``.

    Returns:
        Existing ``.bog-agents/skills`` directories in priority order.
    """
    cwd = user_cwd.resolve()
    if project_root is None:
        candidate = cwd / ".bog-agents" / "skills"
        return [candidate] if candidate.is_dir() else []

    root = project_root.resolve()
    try:
        relative = cwd.relative_to(root)
    except ValueError:
        # cwd is outside the project root — fall back to project + cwd.
        out: list[Path] = []
        proj_skills = root / ".bog-agents" / "skills"
        if proj_skills.is_dir():
            out.append(proj_skills)
        cwd_skills = cwd / ".bog-agents" / "skills"
        if cwd_skills.is_dir() and cwd_skills.resolve() != proj_skills.resolve():
            out.append(cwd_skills)
        return out

    found: list[Path] = []
    accumulated = root
    candidate = accumulated / ".bog-agents" / "skills"
    if candidate.is_dir():
        found.append(candidate)
    for part in relative.parts:
        accumulated = accumulated / part
        candidate = accumulated / ".bog-agents" / "skills"
        if candidate.is_dir():
            # Skip duplicates of the project root entry.
            if found and candidate.resolve() == found[0].resolve():
                continue
            found.append(candidate)
    return found


# Recognised top-level instruction filenames (all matching files in a directory
# load). Superset of Claude Code (CLAUDE.md, CLAUDE.local.md) and the AGENTS.md
# convention (Tier-1 #5 multi-vendor compat).
_AGENT_MD_FILENAMES: tuple[str, ...] = (
    "AGENTS.md",
    "AGENT.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
)

# Vendor-neutral rules directories: every ``*.md`` inside these loads regardless
# of name, so a repo can carry bog / Claude / Cursor rule sets simultaneously.
# ``.cursorrules`` is Cursor's older single-file convention.
_RULES_SUBDIRS: tuple[str, ...] = (
    ".bog-agents/rules",
    ".claude/rules",
    ".cursor/rules",
)
_SINGLE_RULE_FILES: tuple[str, ...] = (".cursorrules",)


def _rule_files_in_dir(directory: Path) -> list[Path]:
    """Return rule files from the vendor-neutral rules dirs inside ``directory``."""
    out: list[Path] = []
    for subdir in _RULES_SUBDIRS:
        rules_dir = directory / subdir
        try:
            if rules_dir.is_dir():
                out.extend(sorted(p for p in rules_dir.glob("*.md") if p.is_file()))
        except OSError:
            pass
    for name in _SINGLE_RULE_FILES:
        candidate = directory / name
        try:
            if candidate.is_file():
                out.append(candidate)
        except OSError:
            pass
    return out


def _agent_md_in_dir(directory: Path) -> list[Path]:
    """Return instruction files inside ``directory`` (multi-vendor; Tier-1 #5).

    Top-level files (`AGENTS.md`, `AGENT.md`, `CLAUDE.md`, `CLAUDE.local.md`)
    come first, then every `*.md` under the vendor-neutral rules directories
    (`.bog-agents/rules/`, `.claude/rules/`, `.cursor/rules/`) plus
    `.cursorrules`.
    """
    out: list[Path] = []
    for name in _AGENT_MD_FILENAMES:
        candidate = directory / name
        try:
            if candidate.is_file():
                out.append(candidate)
        except OSError:
            pass
    out.extend(_rule_files_in_dir(directory))
    return out


def find_hierarchical_agent_md(
    *,
    user_cwd: Path,
    project_root: Path | None,
    home: Path | None = None,
) -> list[Path]:
    """Return AGENTS.md / CLAUDE.md files in cascading precedence order.

    The cascade is:

    1. ``$HOME/.bog-agents/AGENTS.md`` (and ``CLAUDE.md``) — global rules.
    2. Each ancestor of ``user_cwd`` from the project root *down to* the
       parent of ``user_cwd`` (skipping the cwd itself, which lands in
       step 4). Entries are added shallowest-to-deepest so deeper dirs
       win.
    3. Project root files (``project_root/.bog-agents/AGENTS.md``,
       ``project_root/AGENTS.md``, ``project_root/CLAUDE.md``) — see
       :func:`find_project_agent_md`. Skipped if the project root is
       outside the cwd's ancestry (rare but possible).
    4. ``user_cwd/AGENTS.md`` and ``user_cwd/CLAUDE.md`` — the most
       specific files; placed LAST so the model sees them most recently.

    Duplicates are removed while preserving the first occurrence's
    position.

    Args:
        user_cwd: The directory the user invoked the CLI from.
        project_root: The detected project root, or ``None`` if running
            outside a project.
        home: Override for ``Path.home()`` (used by tests).

    Returns:
        Ordered list of existing AGENTS.md / CLAUDE.md paths.
    """
    home_dir = home if home is not None else Path.home()
    cwd = user_cwd.resolve()

    paths: list[Path] = []

    # 1. Home / global rules.
    paths.extend(_agent_md_in_dir(home_dir / ".bog-agents"))
    paths.extend(_agent_md_in_dir(home_dir))

    # 2. Ancestor walk between project root (exclusive) and the cwd
    # (exclusive). Walk shallow → deep so deeper directories appear
    # later in the list and override.
    if project_root is not None:
        try:
            relative = cwd.relative_to(project_root.resolve())
        except ValueError:
            relative = None
    else:
        relative = None

    if project_root is not None and relative is not None:
        # 3. Project root files.
        paths.extend(find_project_agent_md(project_root))
        # 2-cont: walk from project_root deeper toward (but not including) cwd.
        accumulated = project_root.resolve()
        for part in relative.parts[:-1]:
            accumulated = accumulated / part
            paths.extend(_agent_md_in_dir(accumulated))

    # 4. The cwd itself — most specific, last.
    if project_root is None or cwd != project_root.resolve():
        paths.extend(_agent_md_in_dir(cwd))

    # Deduplicate while preserving order.
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(p)
    return deduped
