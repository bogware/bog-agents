"""Event-driven file-change detection backing FILE_CHANGE triggers.

Wraps `watchdog` observers so a file change fires a trigger within the OS's
event latency instead of waiting up to a full scheduler tick, and so a large
watch tree costs nothing per tick (no `os.walk`). Degrades gracefully: if
watchdog is unavailable, or an observer can't attach to a directory, the
manager reports that directory as not-watched and the scheduler falls back to
polling (`scheduler._check_file_trigger`).

The scheduler only activates watching inside its long-running loop
(`run_forever`); a bare `_tick()` (as unit tests drive it) never starts an
observer thread, so the polling path — and its behaviour — is unchanged there.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Give the type checker a single, unambiguous view — the real watchdog types.
    # At runtime the try/except below provides the graceful-degradation fallback.
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    _WATCHDOG_AVAILABLE = True
else:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        _WATCHDOG_AVAILABLE = True
    except Exception:  # pragma: no cover - watchdog is a declared dep; guard for minimal installs
        _WATCHDOG_AVAILABLE = False

        class FileSystemEventHandler:
            """Minimal stand-in so this module imports without watchdog installed."""


# Directories never worth recording events from — mirror the poll-path prune set
# so a recursive watch doesn't fire triggers on VCS/vendored/build churn.
_PRUNE_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache"})

# Only creations/modifications/moves count as a "change"; deletions and
# open/close events are ignored (they don't represent new content to act on).
_RECORD_EVENT_TYPES = frozenset({"created", "modified", "moved"})

# Per-directory ring-buffer size. Bounds memory under a burst of changes; the
# scheduler only needs the most recent change newer than a job's last run.
_MAX_EVENTS_PER_DIR = 4096


class _WatchState:
    """Thread-safe ring buffer of recent file-change events for one directory."""

    def __init__(self, maxlen: int = _MAX_EVENTS_PER_DIR) -> None:
        self._events: deque[tuple[float, str, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, path: str) -> None:
        """Record a change to `path` at the current time (called from the observer thread)."""
        name = Path(path).name
        with self._lock:
            self._events.append((time.time(), name, path))

    def changed_since(self, patterns: list[str], since: float) -> Path | None:
        """Return the most recent changed path newer than `since` matching `patterns`, else None."""
        pats = patterns or ["*"]
        with self._lock:
            # Newest first, so the first match is the most recent change.
            for event_time, name, path in reversed(self._events):
                if event_time > since and any(fnmatch.fnmatch(name, pat) for pat in pats):
                    return Path(path)
        return None


class _ChangeCollector(FileSystemEventHandler):
    """Watchdog handler that funnels relevant file events into a `_WatchState`."""

    def __init__(self, state: _WatchState) -> None:
        self._state = state

    def on_any_event(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        if getattr(event, "event_type", "") not in _RECORD_EVENT_TYPES:
            return
        # For a move, the new location is what changed.
        path = getattr(event, "dest_path", "") or getattr(event, "src_path", "")
        if not path:
            return
        if any(part in _PRUNE_DIRS for part in Path(path).parts):
            return
        self._state.record(path)


class FileWatchManager:
    """Manage watchdog observers for a changing set of watched directories.

    A single background observer thread services every scheduled directory.
    `sync` reconciles the live watch set to the directories the current jobs
    ask for (adding new, dropping removed). `changed_since` answers the
    scheduler's per-trigger query. All methods are safe to call from the event
    loop thread; the observer thread only ever touches the per-directory
    `_WatchState` (which is independently locked).
    """

    def __init__(self, *, max_events_per_dir: int = _MAX_EVENTS_PER_DIR) -> None:
        # `Observer` is a platform-selected factory (a value, not a type), so the
        # handle is held as Any rather than annotated with it directly.
        self._observer: Any = None
        self._watches: dict[str, tuple[object, _WatchState]] = {}
        self._max_events = max_events_per_dir
        self._enabled = _WATCHDOG_AVAILABLE

    @property
    def enabled(self) -> bool:
        """Whether watchdog is available and this manager can watch at all."""
        return self._enabled

    def is_watching(self, watch_dir: str) -> bool:
        """True when `watch_dir` currently has a live observer attached."""
        return watch_dir in self._watches

    def sync(self, watch_dirs: set[str]) -> None:
        """Reconcile live watches to exactly `watch_dirs` (best-effort).

        Directories that can't be watched (missing, or the observer refuses)
        are simply left unwatched, so the scheduler polls them instead.
        """
        if not self._enabled:
            return
        # Drop watches no longer requested.
        for wd in list(self._watches):
            if wd not in watch_dirs:
                watch, _state = self._watches.pop(wd)
                if self._observer is not None:
                    try:
                        self._observer.unschedule(watch)
                    except Exception:
                        logger.debug("Failed to unschedule watch for %s", wd, exc_info=True)
        # Add newly requested watches.
        new_dirs = [wd for wd in watch_dirs if wd not in self._watches]
        if not new_dirs:
            return
        if not self._ensure_observer():
            return
        for wd in new_dirs:
            self._add_watch(wd)

    def changed_since(self, watch_dir: str, patterns: list[str], since: float) -> Path | None:
        """Return a changed path under `watch_dir` newer than `since`, or None."""
        entry = self._watches.get(watch_dir)
        if entry is None:
            return None
        _watch, state = entry
        return state.changed_since(patterns, since)

    def stop(self) -> None:
        """Stop the observer thread and clear all watches (idempotent)."""
        observer = self._observer
        self._observer = None
        self._watches.clear()
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=5)
        except Exception:
            logger.debug("Error stopping file-watch observer", exc_info=True)

    def _ensure_observer(self) -> bool:
        """Start the observer thread if needed; return True when one is running."""
        if self._observer is not None:
            return True
        try:
            self._observer = Observer()
            self._observer.start()
        except Exception:
            logger.warning("Could not start file-watch observer; falling back to polling", exc_info=True)
            self._observer = None
            return False
        return True

    def _add_watch(self, watch_dir: str) -> None:
        """Attach a recursive watch to `watch_dir`, or leave it unwatched on failure."""
        path = Path(watch_dir)
        if not path.is_dir():
            # Not watchable now (may appear later); the scheduler will poll it.
            return
        state = _WatchState(self._max_events)
        try:
            watch = self._observer.schedule(_ChangeCollector(state), str(path), recursive=True)
        except Exception:
            logger.warning("Could not watch %s; falling back to polling for it", watch_dir, exc_info=True)
            return
        self._watches[watch_dir] = (watch, state)
        logger.debug("Watching %s for file changes", watch_dir)
