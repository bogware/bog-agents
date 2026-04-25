"""Project memory file loading for bog-agents-cli.

Merges two memory sources into a single block injected into the agent's
system prompt at session start:

1. **Global memory** — ``~/.bog-agents/memory.md``
   User-level knowledge that applies across all projects (preferences,
   coding style, recurring instructions).

2. **Project memory** — ``.bog-agents.md`` at the git repo root or CWD
   Project-specific context (architecture notes, conventions, team notes,
   things to always remember about this codebase).

Both files are optional. If neither exists the function returns an empty
string so the system prompt is not altered.

Format injected into the prompt::

    ---
    ## Memory

    ### Global Memory (~/.bog-agents/memory.md)
    <contents>

    ### Project Memory (.bog-agents.md)
    <contents>
    ---
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GLOBAL_MEMORY_FILENAME = "memory.md"
_PROJECT_MEMORY_FILENAME = ".bog-agents.md"

_DEFAULT_CONFIG_DIR = Path.home() / ".bog-agents"


def _find_project_root(start: Path) -> Path:
    """Walk up from *start* to find the git repo root.

    Falls back to *start* itself when not inside a git repo.

    Args:
        start: Starting directory.

    Returns:
        The git repository root, or *start* if not in a git repo.
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start.resolve()


def load_project_memory(cwd: str | Path | None = None) -> str:
    """Load and merge global + project memory into a prompt block.

    Args:
        cwd: Working directory used to find the project root.
            Defaults to ``Path.cwd()``.

    Returns:
        A Markdown-formatted memory block ready to append to the system
        prompt, or an empty string when both memory files are absent.
    """
    work_dir = Path(cwd).resolve() if cwd else Path.cwd().resolve()

    global_path = _DEFAULT_CONFIG_DIR / _GLOBAL_MEMORY_FILENAME
    project_root = _find_project_root(work_dir)
    project_path = project_root / _PROJECT_MEMORY_FILENAME

    global_content = _read_memory_file(global_path, label="global memory")
    project_content = _read_memory_file(project_path, label="project memory")

    if not global_content and not project_content:
        return ""

    sections: list[str] = []

    if global_content:
        sections.append(
            f"### Global Memory (`~/.bog-agents/{_GLOBAL_MEMORY_FILENAME}`)\n\n"
            f"{global_content.strip()}"
        )

    if project_content:
        sections.append(
            f"### Project Memory (`.bog-agents.md`)\n\n{project_content.strip()}"
        )

    body = "\n\n".join(sections)
    return f"\n---\n## Memory\n\n{body}\n---\n"


def _read_memory_file(path: Path, *, label: str = "memory") -> str:
    """Read a memory file, returning its content or an empty string on error.

    Args:
        path: Path to the memory file.
        label: Human-readable label for log messages.

    Returns:
        File content string, or empty string if absent or unreadable.
    """
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        logger.debug("Loaded %s from %s (%d chars)", label, path, len(content))
        return content
    except OSError as exc:
        logger.warning("Could not read %s from %s: %s", label, path, exc)
        return ""
