"""Hot-Reload Skills middleware for live skill updates without restart.

Watches skill directories for changes and reloads modified skills
immediately. New or updated SKILL.md files become available to the
agent within seconds — no session restart required.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
DEFAULT_POLL_INTERVAL = 5.0  # seconds


@dataclass
class SkillFileState:
    """Tracked state of a single skill file."""

    path: str
    content_hash: str
    last_modified: float
    loaded_at: float = field(default_factory=time.time)


@dataclass
class HotReloadState:
    """State tracking for hot-reload skill watching."""

    watched_dirs: list[str] = field(default_factory=list)
    skill_states: dict[str, SkillFileState] = field(default_factory=dict)
    last_scan: float = 0.0
    reload_count: int = 0
    errors: list[str] = field(default_factory=list)


def _hash_file(path: str) -> str | None:
    """Compute SHA-256 hash of a file's contents.

    Args:
        path: File path.

    Returns:
        Hex digest or None if file can't be read.
    """
    try:
        content = Path(path).read_bytes()
        return hashlib.sha256(content).hexdigest()
    except OSError as exc:
        logger.debug("Cannot hash %s: %s", path, exc)
        return None


def scan_skill_directories(watch_dirs: list[str]) -> dict[str, SkillFileState]:
    """Scan directories for SKILL.md files and compute their state.

    Args:
        watch_dirs: List of directory paths to scan.

    Returns:
        Dict mapping skill path to its current state.
    """
    states: dict[str, SkillFileState] = {}

    for watch_dir in watch_dirs:
        dir_path = Path(watch_dir)
        if not dir_path.is_dir():
            continue

        for skill_dir in dir_path.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / SKILL_FILENAME
            if not skill_file.is_file():
                continue

            file_path = str(skill_file)
            content_hash = _hash_file(file_path)
            if content_hash is None:
                continue

            try:
                mtime = skill_file.stat().st_mtime
            except OSError:
                continue

            states[file_path] = SkillFileState(
                path=file_path,
                content_hash=content_hash,
                last_modified=mtime,
            )

    return states


def detect_changes(
    old_states: dict[str, SkillFileState],
    new_states: dict[str, SkillFileState],
) -> tuple[list[str], list[str], list[str]]:
    """Detect added, modified, and removed skills.

    Args:
        old_states: Previous skill file states.
        new_states: Current skill file states.

    Returns:
        Tuple of (added_paths, modified_paths, removed_paths).
    """
    old_keys = set(old_states.keys())
    new_keys = set(new_states.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    modified = [
        path for path in old_keys & new_keys
        if old_states[path].content_hash != new_states[path].content_hash
    ]

    return added, modified, removed


class HotReloadSkillsMiddleware(AgentMiddleware):
    """Middleware for live-reloading skills without session restart.

    Watches configured skill directories and detects changes on each
    agent turn. Modified skills are reloaded transparently.

    Example:
        ```python
        from bog_agents.middleware.hot_reload_skills import HotReloadSkillsMiddleware

        middleware = HotReloadSkillsMiddleware(
            watch_dirs=[
                "~/.bog-agents/skills/",
                "./.bog-agents/skills/",
            ],
            poll_interval=5.0,
        )
        ```
    """

    watch_dirs: list[str]
    poll_interval: float
    state: HotReloadState
    _on_reload: Any

    def __init__(
        self,
        *,
        watch_dirs: list[str] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_reload: Any = None,
    ) -> None:
        """Initialize hot-reload skills middleware.

        Args:
            watch_dirs: Directories to watch for skill changes.
            poll_interval: Seconds between directory scans.
            on_reload: Optional callback(added, modified, removed) on changes.
        """
        resolved_dirs: list[str] = []
        for d in (watch_dirs or []):
            resolved = os.path.expanduser(d)
            resolved_dirs.append(resolved)

        self.watch_dirs = resolved_dirs
        self.poll_interval = poll_interval
        self.state = HotReloadState(watched_dirs=resolved_dirs)
        self._on_reload = on_reload

        # Initial scan
        self.state.skill_states = scan_skill_directories(resolved_dirs)
        self.state.last_scan = time.time()
        logger.info(
            "Hot-reload skills: watching %d dirs, found %d skills",
            len(resolved_dirs),
            len(self.state.skill_states),
        )

    def check_for_changes(self) -> tuple[list[str], list[str], list[str]]:
        """Check for skill file changes if poll interval has elapsed.

        Returns:
            Tuple of (added, modified, removed) paths. Empty if no check needed.
        """
        now = time.time()
        if now - self.state.last_scan < self.poll_interval:
            return [], [], []

        self.state.last_scan = now
        new_states = scan_skill_directories(self.watch_dirs)
        added, modified, removed = detect_changes(self.state.skill_states, new_states)

        if added or modified or removed:
            self.state.skill_states = new_states
            self.state.reload_count += 1
            logger.info(
                "Skills changed: +%d modified=%d -%d (reload #%d)",
                len(added), len(modified), len(removed), self.state.reload_count,
            )
            if self._on_reload:
                try:
                    self._on_reload(added, modified, removed)
                except Exception:
                    logger.debug("on_reload callback failed", exc_info=True)
        else:
            self.state.skill_states = new_states

        return added, modified, removed

    @property
    def loaded_skill_count(self) -> int:
        """Number of currently loaded skills."""
        return len(self.state.skill_states)

    @property
    def watched_dir_count(self) -> int:
        """Number of watched directories."""
        return len(self.watch_dirs)

    async def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        call_next: Any,
        runtime: Any,
    ) -> ModelResponse[ResponseT]:
        """Check for skill changes before each model call."""
        self.check_for_changes()
        return await call_next(request, runtime)
