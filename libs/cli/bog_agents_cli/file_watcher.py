"""File system event watcher — triggers pipelines on file changes."""

from __future__ import annotations

import fnmatch
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FileWatchEvent:
    """A file system event that matched a watch pattern.

    Attributes:
        path: Changed file path.
        event_type: One of "created", "modified", "deleted", "moved".
        timestamp: Unix timestamp from `time.time()`.
        pattern: The glob pattern that matched.
    """

    path: str
    event_type: str
    timestamp: float
    pattern: str


@dataclass
class FileWatchConfig:
    """Configuration for a single file-watch trigger.

    Attributes:
        patterns: Glob patterns to watch, e.g. ``["*.py", "src/**/*.ts"]``.
        pipeline_name: Pipeline to trigger when a match fires.
        debounce_seconds: Ignore subsequent events within this window.
        ignore_patterns: Patterns to exclude, e.g. ``["**/__pycache__/**", "*.pyc"]``.
    """

    patterns: list[str]
    pipeline_name: str
    debounce_seconds: float = 2.0
    ignore_patterns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal watchdog handler
# ---------------------------------------------------------------------------


class _BogEventHandler:
    """Watchdog event handler that applies debounce and pattern matching.

    Args:
        configs: Watch configurations to evaluate on every event.
        on_trigger: Callback invoked when an event matches and passes debounce.
    """

    def __init__(
        self,
        configs: list[FileWatchConfig],
        on_trigger: Callable[[FileWatchEvent, FileWatchConfig], None],
    ) -> None:
        self._configs = configs
        self._on_trigger = on_trigger
        # Per-config debounce tracking: key = (pipeline_name, pattern)
        self._last_fired: dict[str, float] = {}

    def _dispatch(self, path: str, event_type: str) -> None:
        """Evaluate *path* against all configs and fire callbacks when due.

        Args:
            path: Absolute or relative path of the changed file.
            event_type: One of "created", "modified", "deleted", "moved".
        """
        now = time.time()
        for config in self._configs:
            # Check ignore patterns first
            if any(fnmatch.fnmatch(path, ig) for ig in config.ignore_patterns):
                continue

            for pattern in config.patterns:
                if not fnmatch.fnmatch(path, pattern):
                    # Also try matching just the filename component
                    filename = Path(path).name
                    if not fnmatch.fnmatch(filename, pattern):
                        continue

                debounce_key = f"{config.pipeline_name}::{pattern}"
                last = self._last_fired.get(debounce_key, 0.0)
                if now - last < config.debounce_seconds:
                    logger.debug(
                        "Debouncing file event %s for pipeline %r (%.1fs remaining)",
                        path,
                        config.pipeline_name,
                        config.debounce_seconds - (now - last),
                    )
                    break

                self._last_fired[debounce_key] = now
                event = FileWatchEvent(
                    path=path,
                    event_type=event_type,
                    timestamp=now,
                    pattern=pattern,
                )
                logger.debug(
                    "File watcher triggered pipeline %r for path %s (pattern %r)",
                    config.pipeline_name,
                    path,
                    pattern,
                )
                try:
                    self._on_trigger(event, config)
                except Exception:
                    logger.debug(
                        "on_trigger callback raised for pipeline %r",
                        config.pipeline_name,
                        exc_info=True,
                    )
                break  # First matching pattern wins per config

    # ------------------------------------------------------------------
    # watchdog FileSystemEventHandler interface (called by Observer)
    # ------------------------------------------------------------------

    def on_created(self, event: Any) -> None:  # noqa: ANN401
        """Handle a file-created event from watchdog."""
        if not getattr(event, "is_directory", False):
            self._dispatch(event.src_path, "created")

    def on_modified(self, event: Any) -> None:  # noqa: ANN401
        """Handle a file-modified event from watchdog."""
        if not getattr(event, "is_directory", False):
            self._dispatch(event.src_path, "modified")

    def on_deleted(self, event: Any) -> None:  # noqa: ANN401
        """Handle a file-deleted event from watchdog."""
        if not getattr(event, "is_directory", False):
            self._dispatch(event.src_path, "deleted")

    def on_moved(self, event: Any) -> None:  # noqa: ANN401
        """Handle a file-moved event from watchdog."""
        if not getattr(event, "is_directory", False):
            self._dispatch(event.dest_path, "moved")


# ---------------------------------------------------------------------------
# Public watcher class
# ---------------------------------------------------------------------------


class BogFileWatcher:
    """File system watcher that triggers pipelines when watched files change.

    Wraps a ``watchdog`` :class:`~watchdog.observers.Observer` in a daemon
    thread.  Multiple :class:`FileWatchConfig` objects can be registered,
    each mapping a set of glob patterns to a pipeline name.

    Args:
        watch_dir: Root directory to watch recursively.
        configs: Initial set of watch configurations.
        on_trigger: Callback invoked with ``(event, config)`` on a match.
    """

    def __init__(
        self,
        watch_dir: Path,
        configs: list[FileWatchConfig],
        on_trigger: Callable[[FileWatchEvent, FileWatchConfig], None],
    ) -> None:
        self._watch_dir = watch_dir
        self._configs: list[FileWatchConfig] = list(configs)
        self._on_trigger = on_trigger
        self._observer: Any = None  # watchdog.observers.Observer
        self._handler: _BogEventHandler | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the watchdog observer in a daemon thread."""
        from watchdog.observers import Observer  # type: ignore[import-untyped]

        self._handler = _BogEventHandler(self._configs, self._on_trigger)
        observer = Observer()
        observer.schedule(self._handler, str(self._watch_dir), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        logger.debug("BogFileWatcher started on %s", self._watch_dir)

    def stop(self) -> None:
        """Stop the watchdog observer and join its thread."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                logger.debug("Error stopping file watcher observer", exc_info=True)
            finally:
                self._observer = None
        logger.debug("BogFileWatcher stopped")

    def is_running(self) -> bool:
        """Return ``True`` if the observer thread is alive.

        Returns:
            Whether the underlying watchdog observer is currently running.
        """
        return self._observer is not None and self._observer.is_alive()

    # ------------------------------------------------------------------
    # Runtime config management
    # ------------------------------------------------------------------

    def add_config(self, config: FileWatchConfig) -> None:
        """Add a new watch configuration at runtime.

        Args:
            config: The watch configuration to add.
        """
        self._configs.append(config)
        if self._handler is not None:
            self._handler._configs = self._configs

    def remove_config(self, pipeline_name: str) -> None:
        """Remove all configurations matching *pipeline_name*.

        Args:
            pipeline_name: Pipeline name to remove.
        """
        self._configs = [c for c in self._configs if c.pipeline_name != pipeline_name]
        if self._handler is not None:
            self._handler._configs = self._configs


# ---------------------------------------------------------------------------
# YAML parsing helper
# ---------------------------------------------------------------------------


def watch_config_from_dict(d: dict) -> FileWatchConfig:
    """Parse a :class:`FileWatchConfig` from a pipeline YAML ``watch:`` block.

    Expected YAML shape::

        watch:
          patterns: ["*.py"]
          debounce_seconds: 3.0
          ignore_patterns: ["**/test_*.py"]

    The ``pipeline_name`` key must be injected by the caller before passing
    to this function.

    Args:
        d: Mapping from the ``watch:`` block, augmented with ``pipeline_name``.

    Returns:
        Parsed :class:`FileWatchConfig`.
    """
    patterns = d.get("patterns", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    ignore_patterns = d.get("ignore_patterns", [])
    if isinstance(ignore_patterns, str):
        ignore_patterns = [ignore_patterns]

    return FileWatchConfig(
        patterns=list(patterns),
        pipeline_name=str(d.get("pipeline_name", "")),
        debounce_seconds=float(d.get("debounce_seconds", 2.0)),
        ignore_patterns=list(ignore_patterns),
    )


# ---------------------------------------------------------------------------
# Status formatting
# ---------------------------------------------------------------------------


def format_watcher_status(watchers: list[BogFileWatcher]) -> str:
    """Return a Rich markup table summarising active :class:`BogFileWatcher` instances.

    Args:
        watchers: Watchers to include in the table.

    Returns:
        Rich markup string containing a formatted table.
    """
    if not watchers:
        return "No file watchers configured."

    rows: list[str] = []
    rows.append(
        "[bold]Watch Dir[/bold] | [bold]Patterns[/bold] | "
        "[bold]Pipeline[/bold] | [bold]Status[/bold] | [bold]Last Fired[/bold]"
    )
    rows.append("-" * 80)

    for watcher in watchers:
        status = (
            "[green]running[/green]" if watcher.is_running() else "[red]stopped[/red]"
        )
        watch_dir = str(watcher._watch_dir)
        for config in watcher._configs:
            patterns_str = ", ".join(config.patterns) if config.patterns else "(none)"
            # Find most recent last_fired for this config across all patterns
            last_fired_ts: float | None = None
            handler = watcher._handler
            if handler is not None:
                for pattern in config.patterns:
                    key = f"{config.pipeline_name}::{pattern}"
                    ts = handler._last_fired.get(key)
                    if ts is not None and (last_fired_ts is None or ts > last_fired_ts):
                        last_fired_ts = ts

            if last_fired_ts is not None:
                last_fired_str = time.strftime(
                    "%H:%M:%S", time.localtime(last_fired_ts)
                )
            else:
                last_fired_str = "(never)"

            rows.append(
                f"{watch_dir} | {patterns_str} | {config.pipeline_name} | {status} | {last_fired_str}"
            )

    return "\n".join(rows)
