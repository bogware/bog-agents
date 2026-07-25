"""Tests for event-driven file-change detection (watchdog) and its fallbacks."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bog_agents_daemon.file_watch import (
    _WATCHDOG_AVAILABLE,
    FileWatchManager,
    _ChangeCollector,
    _WatchState,
)
from bog_agents_daemon.models import AmbientJob, TriggerConfig, TriggerType
from bog_agents_daemon.scheduler import DaemonScheduler


async def _noop_runner(job: AmbientJob, *, trigger_type: object, trigger_context: object = None) -> None:
    pass


class _FakeEvent:
    """Stand-in for a watchdog FileSystemEvent."""

    def __init__(self, *, event_type: str = "modified", is_directory: bool = False, src_path: str = "", dest_path: str = "") -> None:
        self.event_type = event_type
        self.is_directory = is_directory
        self.src_path = src_path
        self.dest_path = dest_path


class TestWatchState:
    def test_records_and_matches(self) -> None:
        st = _WatchState()
        st.record("/proj/app.py")
        assert st.changed_since(["*.py"], 0) == Path("/proj/app.py")

    def test_ignores_events_before_since(self) -> None:
        st = _WatchState()
        st.record("/proj/app.py")
        assert st.changed_since(["*.py"], time.time() + 10) is None

    def test_pattern_filter(self) -> None:
        st = _WatchState()
        st.record("/proj/data.csv")
        assert st.changed_since(["*.py"], 0) is None
        assert st.changed_since(["*.csv"], 0) == Path("/proj/data.csv")

    def test_newest_match_wins(self) -> None:
        st = _WatchState()
        st.record("/proj/old.py")
        time.sleep(0.01)
        st.record("/proj/new.py")
        assert st.changed_since(["*.py"], 0) == Path("/proj/new.py")

    def test_empty_patterns_matches_anything(self) -> None:
        st = _WatchState()
        st.record("/proj/whatever.bin")
        assert st.changed_since([], 0) == Path("/proj/whatever.bin")


class TestChangeCollector:
    def test_records_modified_file(self) -> None:
        st = _WatchState()
        _ChangeCollector(st).on_any_event(_FakeEvent(event_type="modified", src_path="/p/a.py"))
        assert st.changed_since(["*.py"], 0) == Path("/p/a.py")

    def test_ignores_directory_events(self) -> None:
        st = _WatchState()
        _ChangeCollector(st).on_any_event(_FakeEvent(is_directory=True, src_path="/p/sub"))
        assert st.changed_since(["*"], 0) is None

    def test_ignores_deletions(self) -> None:
        st = _WatchState()
        _ChangeCollector(st).on_any_event(_FakeEvent(event_type="deleted", src_path="/p/gone.py"))
        assert st.changed_since(["*"], 0) is None

    def test_ignores_pruned_dirs(self) -> None:
        st = _WatchState()
        _ChangeCollector(st).on_any_event(_FakeEvent(event_type="modified", src_path="/p/.git/index"))
        _ChangeCollector(st).on_any_event(_FakeEvent(event_type="modified", src_path="/p/node_modules/x/y.js"))
        assert st.changed_since(["*"], 0) is None

    def test_move_uses_dest_path(self) -> None:
        st = _WatchState()
        _ChangeCollector(st).on_any_event(_FakeEvent(event_type="moved", src_path="/p/old.py", dest_path="/p/new.py"))
        assert st.changed_since(["*.py"], 0) == Path("/p/new.py")


class TestFileWatchManager:
    def test_not_watching_before_sync(self, tmp_path: Path) -> None:
        mgr = FileWatchManager()
        assert mgr.is_watching(str(tmp_path)) is False
        assert mgr.changed_since(str(tmp_path), ["*"], 0) is None

    def test_disabled_manager_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mgr = FileWatchManager()
        monkeypatch.setattr(mgr, "_enabled", False)
        mgr.sync({str(tmp_path)})
        assert mgr.is_watching(str(tmp_path)) is False

    @pytest.mark.skipif(not _WATCHDOG_AVAILABLE, reason="watchdog not installed")
    def test_sync_adds_and_removes_watches(self, tmp_path: Path) -> None:
        mgr = FileWatchManager()
        d1, d2 = tmp_path / "a", tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        try:
            mgr.sync({str(d1), str(d2)})
            assert mgr.is_watching(str(d1))
            assert mgr.is_watching(str(d2))
            mgr.sync({str(d1)})
            assert mgr.is_watching(str(d1))
            assert mgr.is_watching(str(d2)) is False
        finally:
            mgr.stop()
        assert mgr.is_watching(str(d1)) is False

    @pytest.mark.skipif(not _WATCHDOG_AVAILABLE, reason="watchdog not installed")
    def test_detects_real_file_change(self, tmp_path: Path) -> None:
        mgr = FileWatchManager()
        since = time.time()
        try:
            mgr.sync({str(tmp_path)})
            assert mgr.is_watching(str(tmp_path))
            time.sleep(0.3)  # let the observer settle before mutating
            (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")

            hit = None
            deadline = time.time() + 5.0
            while time.time() < deadline:
                hit = mgr.changed_since(str(tmp_path), ["*.py"], since)
                if hit is not None:
                    break
                time.sleep(0.05)
            assert hit is not None, "watchdog did not deliver a change event within 5s"
            assert hit.name == "app.py"
        finally:
            mgr.stop()


class TestSchedulerFileDetection:
    """The scheduler's _detect_file_change falls back to polling correctly."""

    def test_never_run_uses_poll(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        sched = DaemonScheduler(store_loader=list, runner=_noop_runner)
        trigger = TriggerConfig(type=TriggerType.FILE_CHANGE, watch_dir=str(tmp_path), watch_patterns=["*.py"])
        # last_run_at == 0 → poll path sees the pre-existing file.
        assert sched._detect_file_change(trigger, 0) is not None

    def test_not_watching_uses_poll(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        sched = DaemonScheduler(store_loader=list, runner=_noop_runner)
        trigger = TriggerConfig(type=TriggerType.FILE_CHANGE, watch_dir=str(tmp_path), watch_patterns=["*.py"])
        # No observer attached (watching never activated) → poll path; the file's
        # mtime is newer than the ancient last_run, so it is detected.
        assert sched._detect_file_change(trigger, 1.0) is not None

    def test_no_watch_dir_returns_none(self) -> None:
        sched = DaemonScheduler(store_loader=list, runner=_noop_runner)
        trigger = TriggerConfig(type=TriggerType.FILE_CHANGE, watch_dir="")
        assert sched._detect_file_change(trigger, 0) is None
