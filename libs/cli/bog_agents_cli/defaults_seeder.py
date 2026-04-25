"""Default content seeder — ships built-in prompts and pipelines to ``~/.bog-agents/``.

On first run (and again after a package version bump) this module copies the
bundled defaults into the user's data directory using an **additive-merge**
strategy: existing user content is never overwritten.

Sentinel file: ``~/.bog-agents/.bog_seeded_vX.Y.Z`` — when this file exists for
the current package version the seeder skips work entirely.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to the bundled defaults directory (inside the installed package)
_DEFAULTS_DIR = Path(__file__).parent / "defaults"
_BOG_DIR = Path.home() / ".bog-agents"
_PROMPTS_PATH = _BOG_DIR / "prompt_library.toml"
_PIPELINES_DIR = _BOG_DIR / "pipelines"


def _sentinel_path(version: str) -> Path:
    return _BOG_DIR / f".bog_seeded_v{version}"


def seed_if_needed() -> None:
    """Seed default content to ``~/.bog-agents/`` if not already done for this version.

    Reads the current package version from :mod:`bog_agents_cli._version` and
    checks for a sentinel file.  If the sentinel is absent the bundled defaults
    are merged in additively — prompt keys and pipeline files that already exist
    on disk are never touched.

    This function swallows all errors so a broken seed never prevents the app
    from starting.
    """
    try:
        from bog_agents_cli._version import __version__

        sentinel = _sentinel_path(__version__)
        if sentinel.exists():
            return  # Already seeded for this version

        logger.debug("Seeding default content for bog-agents-cli v%s", __version__)
        _BOG_DIR.mkdir(parents=True, exist_ok=True)

        _seed_prompts()
        _seed_pipelines()

        # Write sentinel so we don't repeat on next startup
        sentinel.touch()
        logger.info("Default content seeded for v%s", __version__)
    except Exception:
        logger.warning("Default content seeding failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Prompt seeding
# ---------------------------------------------------------------------------


def _seed_prompts() -> None:
    """Merge bundled prompt_library.toml into the user's library (additive only)."""
    import tomllib

    import tomli_w

    bundled_path = _DEFAULTS_DIR / "prompt_library.toml"
    if not bundled_path.exists():
        return

    # Load bundled defaults
    with bundled_path.open("rb") as fh:
        bundled = tomllib.load(fh)
    bundled_prompts: dict = bundled.get("prompts", {})
    if not bundled_prompts:
        return

    # Load existing user library (may not exist yet)
    existing: dict = {}
    if _PROMPTS_PATH.exists():
        try:
            with _PROMPTS_PATH.open("rb") as fh:
                existing = tomllib.load(fh)
        except Exception:
            logger.warning("Could not read existing prompt library; will create fresh")

    user_prompts: dict = existing.setdefault("prompts", {})

    added = 0
    for name, entry in bundled_prompts.items():
        if name not in user_prompts:
            user_prompts[name] = entry
            added += 1

    if added:
        _PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PROMPTS_PATH.open("wb") as fh:
            tomli_w.dump(existing, fh)
        logger.debug("Seeded %d new default prompts", added)


# ---------------------------------------------------------------------------
# Pipeline seeding
# ---------------------------------------------------------------------------


def _seed_pipelines() -> None:
    """Copy bundled pipeline YAML files that don't yet exist in the user's directory."""
    bundled_pipelines = _DEFAULTS_DIR / "pipelines"
    if not bundled_pipelines.exists():
        return

    _PIPELINES_DIR.mkdir(parents=True, exist_ok=True)

    added = 0
    for src in bundled_pipelines.glob("*.yaml"):
        dest = _PIPELINES_DIR / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
            added += 1

    if added:
        logger.debug("Seeded %d new default pipelines", added)
