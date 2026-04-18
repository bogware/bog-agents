"""CLI helpers for displaying and refreshing the repository map.

Called from the `/repomap` slash command handler in app.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def get_repo_map_text(cwd: str | Path, *, refresh: bool = False) -> str:
    """Return the formatted repo map for the current project.

    Args:
        cwd: Project root directory.
        refresh: Force a full rebuild, ignoring the cache.

    Returns:
        Formatted repo map string.
    """
    try:
        from bog_agents.middleware.repo_map import build_repo_map_cached

        return build_repo_map_cached(Path(cwd), force_rebuild=refresh)
    except Exception as exc:  # noqa: BLE001
        return f"Error building repo map: {exc}"


def get_repo_map_stats(cwd: str | Path) -> dict[str, Any]:
    """Return statistics about the current repo map cache.

    Args:
        cwd: Project root directory.

    Returns:
        Dict with cached, file_count, built_at, cache_path.
    """
    try:
        from bog_agents.middleware.repo_map import get_repo_map_stats as _stats

        return _stats(Path(cwd))
    except Exception:  # noqa: BLE001
        return {"cached": False, "file_count": 0, "built_at": 0.0, "cache_path": ""}


def format_repo_map_status(cwd: str | Path) -> str:
    """Return a one-line status summary for the repo map.

    Args:
        cwd: Project root directory.

    Returns:
        Human-readable status string.
    """
    stats = get_repo_map_stats(cwd)
    if not stats.get("cached"):
        return "Repo map not yet built. Run [bold]/repomap[/bold] to index the project."

    file_count = stats.get("file_count", 0)
    built_at = stats.get("built_at", 0.0)
    age_secs = int(time.time() - built_at)

    if age_secs < 60:
        age = "just now"
    elif age_secs < 3600:
        age = f"{age_secs // 60}m ago"
    elif age_secs < 86400:
        age = f"{age_secs // 3600}h ago"
    else:
        age = f"{age_secs // 86400}d ago"

    return f"{file_count} symbols indexed ({age})"
