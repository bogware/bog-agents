"""Persona / output-style management.

A persona is a markdown file under ``.bog-agents/personas/`` (project) or
``~/.bog-agents/personas/`` (user) with optional frontmatter::

    ---
    name: terse-mentor
    description: Short, direct, and didactic — explains the why
    ---

    # System prompt addendum

    Respond in two beats: the answer, then a one-line "why this matters".
    Never apologize for asking clarifying questions.

The body becomes a system-prompt addendum the agent prepends to its
existing instructions. ``/persona`` toggles the active persona; the
applied prompt fragment is forwarded with the next user message.

This module is independent of the agent runtime so it can be unit-tested
without spinning up a graph.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


_PERSONAS_DIRNAME = "personas"
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*:\s*(.*)$")


@dataclass(frozen=True, slots=True)
class Persona:
    """One discovered persona definition."""

    id: str
    name: str
    description: str
    body: str
    source: Path

    @property
    def system_addendum(self) -> str:
        """The body, ready to append to the agent's system prompt."""
        return self.body.strip()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---``-delimited frontmatter from the body.

    Returns:
        Tuple of (frontmatter dict, body string). Empty frontmatter dict
        if the document has no fence.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    raw_front = match.group(1)
    body = match.group(2)
    fields: dict[str, str] = {}
    for line in raw_front.splitlines():
        line_match = _FRONTMATTER_FIELD_RE.match(line)
        if not line_match:
            continue
        key, value = line_match.group(1), line_match.group(2).strip()
        # Strip optional surrounding quotes.
        if value.startswith(("'", '"')) and value.endswith(value[0]):
            value = value[1:-1]
        fields[key.lower()] = value
    return fields, body


def _persona_dirs(*, project_root: Path | None, home: Path | None = None) -> list[Path]:
    """Return persona directories in priority order (low → high).

    Higher-priority directories override personas with the same id from
    lower-priority ones.
    """
    home_dir = home if home is not None else Path.home()
    dirs: list[Path] = [home_dir / ".bog-agents" / _PERSONAS_DIRNAME]
    if project_root is not None:
        dirs.append(project_root / ".bog-agents" / _PERSONAS_DIRNAME)
    return dirs


def discover_personas(
    *,
    project_root: Path | None,
    home: Path | None = None,
) -> dict[str, Persona]:
    """Find every persona markdown file and return ``{id: Persona}``.

    Project personas override user personas with the same id (matching the
    "closer dir wins" pattern used elsewhere in the CLI). Files that
    cannot be read are logged at DEBUG and skipped.

    Args:
        project_root: Detected project root, or ``None``.
        home: Override for ``Path.home()``.

    Returns:
        Mapping from persona id to :class:`Persona` instance.
    """
    found: dict[str, Persona] = {}
    for directory in _persona_dirs(project_root=project_root, home=home):
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or entry.suffix.lower() != ".md":
                continue
            persona = _load_one(entry)
            if persona is not None:
                found[persona.id] = persona
    return found


def _load_one(path: Path) -> Persona | None:
    """Read one persona file. Returns None on read/parse errors."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("could not read persona %s", path, exc_info=True)
        return None

    front, body = _parse_frontmatter(raw)
    persona_id = front.get("id") or front.get("name") or path.stem
    persona_id = persona_id.strip().lower()
    if not persona_id:
        return None
    name = front.get("name", path.stem)
    description = front.get("description", "")
    return Persona(
        id=persona_id,
        name=name,
        description=description,
        body=body,
        source=path,
    )


def get_persona(
    persona_id: str,
    *,
    project_root: Path | None,
    home: Path | None = None,
) -> Persona | None:
    """Return one persona by id (case-insensitive), or ``None``."""
    return discover_personas(project_root=project_root, home=home).get(
        persona_id.strip().lower()
    )


__all__ = [
    "Persona",
    "discover_personas",
    "get_persona",
]
