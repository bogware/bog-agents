"""Shared settings-cascade reader for layered JSON configs.

Multiple subsystems (auto_mode, peat persona, mcp, etc.) had been
re-implementing the same cascade walker — read user settings.json,
overlay project settings.json, drop sections that don't match, log a
warning when the file is too large or malformed.

This module is the one place to maintain that walker. Each caller only
provides:

- a *section name* (the top-level key inside settings.json)
- a *merge function* that takes (current, override_section) → current

The cascade order (lower-precedence first):

1. Built-in defaults (caller's responsibility — passed as the initial
   ``current`` value)
2. ``~/.bog-agents/settings.json`` — user global
3. ``<project>/.bog-agents/settings.json`` — project local

Any layer that fails to parse logs a warning and is skipped. Files
larger than ``SETTINGS_FILE_MAX_BYTES`` are skipped entirely.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from bog_agents_cli._constants import SETTINGS_FILE_MAX_BYTES

logger = logging.getLogger(__name__)

T = TypeVar("T")


def load_layered_section(
    *,
    section: str,
    initial: T,
    merge: Callable[[T, dict[str, Any]], T],
    project_root: Path | None = None,
    user_home: Path | None = None,
) -> T:
    """Walk the cascade and return the merged result for ``section``.

    Args:
        section: Top-level key inside settings.json that this caller cares
            about (e.g. ``"auto_mode"``, ``"peat"``).
        initial: Starting value (built-in defaults). Returned unchanged
            when no settings files exist.
        merge: Callback invoked once per layer that has a matching dict
            section. Signature ``(current, override_dict) -> new_current``.
            The callback is responsible for *what* counts as a merge —
            extend-vs-replace, type coercion, etc.
        project_root: Optional project directory whose
            ``.bog-agents/settings.json`` will be read AFTER the user
            home file.
        user_home: Override for the user home directory. Tests pass this;
            production callers leave None to use ``Path.home()``.

    Returns:
        The cascade-merged value.
    """
    home = user_home if user_home is not None else Path.home()
    paths: list[Path] = [home / ".bog-agents" / "settings.json"]
    if project_root is not None:
        paths.append(project_root / ".bog-agents" / "settings.json")

    current = initial
    for path in paths:
        current = _apply_one(current, path, section, merge)
    return current


def _apply_one(
    current: T,
    path: Path,
    section: str,
    merge: Callable[[T, dict[str, Any]], T],
) -> T:
    """Apply a single settings file's section to ``current``.

    Robust against missing files, oversized files, malformed JSON,
    non-dict top-level, and missing/non-dict section. None of these
    cases raise — they all log a warning and return ``current``
    unchanged.
    """
    if not path.is_file():
        return current
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("settings: failed to read %s: %s", path, exc)
        return current
    if len(raw) > SETTINGS_FILE_MAX_BYTES:
        logger.warning(
            "settings: %s is %d bytes (> %d cap) — skipping",
            path,
            len(raw),
            SETTINGS_FILE_MAX_BYTES,
        )
        return current
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("settings: failed to parse %s: %s", path, exc)
        return current
    if not isinstance(data, dict):
        logger.warning("settings: top-level of %s is not an object — skipping", path)
        return current
    raw_section = data.get(section)
    if not isinstance(raw_section, dict) or not raw_section:
        return current
    try:
        return merge(current, raw_section)
    except Exception:
        # A buggy merge function shouldn't take down whoever called us.
        # Log + return the value the caller already had.
        logger.warning(
            "settings: merge failed for section %r in %s; layer skipped",
            section,
            path,
            exc_info=True,
        )
        return current
