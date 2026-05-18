"""Project memory file loading for bog-agents-cli.

Merges global + hierarchical project memory into a single block injected
into the agent's system prompt at session start.

Sources, in order from outermost to innermost (LLMs read most-recent
context last so the inner-most rules are emphasized):

1. **Global memory** — ``~/.bog-agents/memory.md``
   User-level knowledge that applies across all projects.

2. **Project memory cascade** — every ``AGENTS.md``, ``CLAUDE.md``, and
   ``.bog-agents.md`` from the git repo root down through each
   intermediate directory to the cwd. The walk starts at the git root
   (so corporate / personal files outside the repo never leak in) and
   ends at the cwd (so the closest directory's rules apply most
   directly). Files at the same depth are emitted in the order
   ``AGENTS.md`` → ``CLAUDE.md`` → ``.bog-agents.md``.

This implements REVIEW.md T-4: hierarchical cascade that picks up
``AGENTS.md`` (the cross-tool standard adopted by Claude Code, Cursor,
Windsurf, Aider, Zed, Warp, Roo Code as of 2025) plus our existing
``.bog-agents.md`` for backward compatibility.

Format injected into the prompt::

    ---
    ## Memory

    ### Global Memory (~/.bog-agents/memory.md)
    <contents>

    ### AGENTS.md (repo root)
    <contents>

    ### CLAUDE.md (libs/cli/)
    <contents>
    ---
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GLOBAL_MEMORY_FILENAME = "memory.md"

# Per-directory memory filenames in emission order. AGENTS.md leads
# because it's the open cross-tool standard; CLAUDE.md follows for
# Claude-Code compatibility; .bog-agents.md is the historical name we
# keep supporting so existing projects don't break.
_PROJECT_MEMORY_FILENAMES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    ".bog-agents.md",
)

_DEFAULT_CONFIG_DIR = Path.home() / ".bog-agents"

# Hard cap on the cascade depth — protects against pathological symlink
# loops or absurdly deep working directories. The cascade also stops at
# the repo root regardless.
_MAX_CASCADE_DEPTH = 32


# ---------------------------------------------------------------------------
# Project-root discovery
# ---------------------------------------------------------------------------


def _find_project_root(start: Path) -> Path:
    """Walk up from *start* to find the git repo root.

    Falls back to *start* itself when not inside a git repo. The cascade
    relies on this to bound how far up the filesystem the walk goes —
    we never want to read someone's ``~/AGENTS.md`` or ``/etc/CLAUDE.md``
    by accident.

    Args:
        start: Starting directory.

    Returns:
        The git repository root, or *start* if not in a git repo.
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


# ---------------------------------------------------------------------------
# Cascade walk
# ---------------------------------------------------------------------------


def _cascade_directories(cwd: Path, project_root: Path) -> list[Path]:
    """Return the directory chain from *project_root* down to *cwd*.

    The output is ordered from outermost (project_root) to innermost
    (cwd). When *cwd* is identical to *project_root* the chain is a
    single-element list with just that directory.

    When *cwd* is **outside** *project_root* (the no-git fallback hit a
    parent that doesn't contain *cwd*), we return just [cwd].

    Args:
        cwd: Working directory.
        project_root: Repo root (or fallback root).

    Returns:
        Ordered directory list. Depth is bounded by
        :data:`_MAX_CASCADE_DEPTH`.
    """
    cwd = cwd.resolve()
    project_root = project_root.resolve()
    try:
        relative = cwd.relative_to(project_root)
    except ValueError:
        return [cwd]
    chain: list[Path] = [project_root]
    accumulator = project_root
    for part in relative.parts:
        accumulator = accumulator / part
        chain.append(accumulator)
    if len(chain) > _MAX_CASCADE_DEPTH:
        chain = chain[:_MAX_CASCADE_DEPTH]
    return chain


def _collect_memory_files(
    cwd: Path,
    project_root: Path,
) -> list[tuple[Path, str]]:
    """Return ``(absolute_path, label)`` for each memory file in the cascade.

    *label* is the relative-to-project-root path for ergonomic display
    (e.g. ``"AGENTS.md (libs/cli/)"``).

    Files are emitted in order: walk depth, then per-directory filename
    order from :data:`_PROJECT_MEMORY_FILENAMES`. Symlinked or
    non-regular paths are filtered out so a malicious in-repo symlink
    can't redirect us at ``/etc/passwd``.
    """
    seen: set[Path] = set()
    out: list[tuple[Path, str]] = []
    for directory in _cascade_directories(cwd, project_root):
        if not directory.is_dir():
            continue
        for filename in _PROJECT_MEMORY_FILENAMES:
            candidate = directory / filename
            # Reject symlinks: ``.bog-agents.md -> ~/.ssh/id_rsa`` is
            # cute and we don't want to load it.
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
            except OSError:
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                relative_dir = directory.relative_to(project_root)
                where = (
                    "repo root"
                    if not relative_dir.parts
                    else f"{relative_dir.as_posix()}/"
                )
            except ValueError:
                where = str(directory)
            out.append((candidate, f"{filename} ({where})"))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_project_memory(cwd: str | Path | None = None) -> str:
    """Load and merge global + cascaded project memory into a prompt block.

    Args:
        cwd: Working directory used to find the project root. Defaults
            to ``Path.cwd()``. The cascade is built from the project
            root down to this directory.

    Returns:
        A Markdown-formatted memory block ready to append to the system
        prompt, or an empty string when every source is absent.
    """
    work_dir = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    project_root = _find_project_root(work_dir)

    sections: list[str] = []

    global_path = _DEFAULT_CONFIG_DIR / _GLOBAL_MEMORY_FILENAME
    global_content = _read_memory_file(global_path, label="global memory")
    if global_content:
        sections.append(
            f"### Global Memory (`~/.bog-agents/{_GLOBAL_MEMORY_FILENAME}`)\n\n"
            f"{global_content.strip()}"
        )

    for path, display_label in _collect_memory_files(work_dir, project_root):
        content = _read_memory_file(path, label=display_label)
        if not content:
            continue
        sections.append(f"### {display_label}\n\n{content.strip()}")

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return f"\n---\n## Memory\n\n{body}\n---\n"


def collect_memory_sources(cwd: str | Path | None = None) -> list[Path]:
    """Return absolute paths of every memory file the cascade would load.

    Useful for the CLI's ``/doctor`` and ``/init`` flows to show users
    what's being injected without dumping the full text.
    """
    work_dir = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    project_root = _find_project_root(work_dir)
    return [
        path
        for path, _label in _collect_memory_files(work_dir, project_root)
        if path.is_file()
    ]


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _read_memory_file(path: Path, *, label: str = "memory") -> str:
    """Read a memory file, returning its content or an empty string on error."""
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s from %s: %s", label, path, exc)
        return ""
    logger.debug("Loaded %s from %s (%d chars)", label, path, len(content))
    return content
