"""Shared logging configuration for file-based tracing.

Two tiers of file logging are available:

1. **Always-on** (``WARNING`` and above) — writes to
   ``~/.bog-agents/logs/bog_agents.log``. Rotated automatically so it
   never grows unbounded. No env-var needed; this is the default on
   every session so that errors and stack traces are always captured.

2. **Verbose debug** — activated by setting the ``BOG_AGENTS_DEBUG``
   environment variable. Lowers the file-log level to ``DEBUG`` and
   captures every log record for detailed post-mortem analysis. The
   file path can be overridden with ``BOG_AGENTS_DEBUG_FILE``.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path.home() / ".bog-agents" / "logs"
_DEFAULT_LOG_FILE = _LOG_DIR / "bog_agents.log"
_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB per file
_BACKUP_COUNT = 3  # keep up to 3 rotated files
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Module-level flag to avoid re-creating the shared handler for every caller.
_shared_handler: RotatingFileHandler | None = None


def _get_log_path() -> Path:
    """Return the log file path, respecting env-var overrides.

    Returns:
        Resolved log file path.
    """
    override = os.environ.get("BOG_AGENTS_DEBUG_FILE")
    if override:
        return Path(override)
    return _DEFAULT_LOG_FILE


def _ensure_shared_handler() -> RotatingFileHandler | None:
    """Create (once) and return the shared rotating file handler.

    Returns:
        The shared handler, or ``None`` if the log file could not be opened.
    """
    global _shared_handler  # noqa: PLW0603
    if _shared_handler is not None:
        return _shared_handler

    log_path = _get_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        print(  # noqa: T201
            f"Warning: could not open log file {log_path}: {exc}",
            file=sys.stderr,
        )
        return None

    is_debug = bool(os.environ.get("BOG_AGENTS_DEBUG"))
    handler.setLevel(logging.DEBUG if is_debug else logging.WARNING)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    _shared_handler = handler
    return handler


def configure_debug_logging(target: logging.Logger) -> None:
    """Attach the shared file handler to *target*.

    Always attaches at ``WARNING`` level so errors are captured without any
    configuration. When ``BOG_AGENTS_DEBUG`` is set, the handler level
    drops to ``DEBUG`` and the logger itself is set to ``DEBUG`` so that
    every record is written.

    Safe to call multiple times for the same logger — the shared handler
    is only added once.

    Args:
        target: Logger to configure.
    """
    handler = _ensure_shared_handler()
    if handler is None:
        return

    # Avoid adding the same handler twice when called from multiple modules.
    if handler in target.handlers:
        return

    target.addHandler(handler)

    is_debug = bool(os.environ.get("BOG_AGENTS_DEBUG"))
    if is_debug:
        target.setLevel(logging.DEBUG)


def get_log_path() -> Path:
    """Return the active log file path for display in the UI.

    Returns:
        The path to the current log file.
    """
    return _get_log_path()
