"""User-configurable prompt overrides for slash commands.

Prompts are loaded from the ``[prompts]`` section of
``~/.bog-agents/config.toml``. When a key matches a slash command name
(without the leading ``/``), the custom prompt replaces the built-in
default.

Example config::

    [prompts]
    init = "Scan this repo and write AGENTS.md with my preferred sections..."
    onboard = "Walk me through the codebase focusing on the API layer..."
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from bog_agents_cli._debug import configure_debug_logging

logger = logging.getLogger(__name__)
configure_debug_logging(logger)

_DEFAULT_CONFIG_PATH = Path.home() / ".bog-agents" / "config.toml"


def load_custom_prompts(config_path: Path | None = None) -> dict[str, str]:
    """Load custom prompts from config.toml.

    Args:
        config_path: Path to config file. Defaults to ``~/.bog-agents/config.toml``.

    Returns:
        Mapping of command name to custom prompt string.
    """
    path = config_path or _DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        logger.warning("Could not load custom prompts from %s: %s", path, e)
        return {}
    prompts = data.get("prompts", {})
    if not isinstance(prompts, dict):
        logger.warning("[prompts] section in config.toml should be a table; ignoring")
        return {}
    return {k: str(v) for k, v in prompts.items() if isinstance(v, str)}


def get_prompt(command: str, default: str) -> str:
    """Get the prompt for a slash command, using custom override if available.

    Args:
        command: The slash command name without the leading ``/``
            (e.g., ``"init"``, ``"onboard"``).
        default: The built-in default prompt to use when no override exists.

    Returns:
        The custom prompt if configured, otherwise the default.
    """
    custom = load_custom_prompts()
    if command in custom:
        logger.info("Using custom prompt for /%s from config.toml", command)
        return custom[command]
    return default


def save_custom_prompt(command: str, prompt: str, config_path: Path | None = None) -> None:
    """Save a custom prompt override to config.toml.

    Creates the ``[prompts]`` section if it doesn't exist. Preserves all
    other config sections.

    Args:
        command: The slash command name without the leading ``/``.
        prompt: The custom prompt string to save.
        config_path: Path to config file. Defaults to ``~/.bog-agents/config.toml``.
    """
    path = config_path or _DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config
    data: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError):
            pass

    prompts = data.setdefault("prompts", {})
    prompts[command] = prompt

    # Write back — tomllib is read-only, so we use a simple writer
    _write_toml(path, data)
    logger.info("Saved custom prompt for /%s to %s", command, path)


def _write_toml(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a dict to a TOML file.

    Uses temp-file-then-rename to prevent corruption if the write is
    interrupted (e.g. crash, power loss, concurrent CLI instances).

    Args:
        path: File path to write.
        data: Dictionary to serialize.
    """
    import contextlib
    import os
    import tempfile

    import tomli_w

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        Path(tmp_path).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
        raise
