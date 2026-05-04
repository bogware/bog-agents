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

        # One-time migrations run BEFORE the version-gated seed so they
        # apply even when the user is already on the current version.
        # Each migration uses its own sentinel.
        _migrate_disable_default_watchers()

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
# One-time migrations
# ---------------------------------------------------------------------------


def _migrate_disable_default_watchers() -> None:
    """Disable the auto-fire ``watch:`` block on the seeded README pipeline.

    A previous default of the ``readme-auto-updater.yaml`` pipeline shipped
    with an enabled ``watch:`` block. That meant any first-time user got
    a file watcher firing on every save the moment they opened the CLI —
    surprising and invasive.

    We've removed the ``watch:`` block from the bundled default. This
    migration brings the user's already-on-disk copy in line *only when
    it matches the unmodified shipped version byte-for-byte* (so we
    never silently rewrite a customised file). Sentinel:
    ``~/.bog-agents/.bog_migration_disable_default_watcher``.
    """
    sentinel = _BOG_DIR / ".bog_migration_disable_default_watcher"
    if sentinel.exists():
        return
    target = _PIPELINES_DIR / "readme-auto-updater.yaml"
    if not target.is_file():
        # Either never seeded, or already deleted by the user. Mark
        # done so we don't re-check on every startup.
        try:
            _BOG_DIR.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
        except OSError:
            pass
        return
    try:
        current = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("watcher migration: could not read %s: %s", target, exc)
        return
    # Detect the exact shipped header line. A user who edited the
    # patterns or commented out the block has either of these tweaked.
    bad_marker = 'watch:\n  patterns: ["*.py", "pyproject.toml", "*.md"]'
    if bad_marker not in current:
        # Either an older pre-watch version OR a user-customised file.
        # Either way leave it alone.
        try:
            sentinel.touch()
        except OSError:
            pass
        return
    # Replace the bad block with a comment-disabled version that
    # preserves the user's view of what they had configured.
    fixed = current.replace(
        'watch:\n  patterns: ["*.py", "pyproject.toml", "*.md"]\n'
        "  debounce_seconds: 300.0\n"
        '  ignore_patterns: ["README.md", "**/test_*.py", "**/__pycache__/**"]',
        "# watch block auto-disabled by one-time migration to stop a default\n"
        "# pipeline from firing uninvited. Uncomment to re-enable:\n"
        "# watch:\n"
        '#   patterns: ["*.py", "pyproject.toml", "*.md"]\n'
        "#   debounce_seconds: 300.0\n"
        '#   ignore_patterns: ["README.md", "**/test_*.py", "**/__pycache__/**"]',
    )
    try:
        target.write_text(fixed, encoding="utf-8")
        sentinel.touch()
        logger.info(
            "Disabled the default readme-auto-updater watch block in %s",
            target,
        )
    except OSError as exc:
        logger.debug("watcher migration: could not write %s: %s", target, exc)


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
