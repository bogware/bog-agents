"""Hardening tests for `PersistentJobsManager` (S32).

Regression coverage for a startup crash: `_load_persisted_jobs` is called
unconditionally from `__init__`, so a single unreadable/locked job file must be
skipped (logged at debug) rather than propagating an ``OSError`` and bricking
the whole persistent-jobs subsystem, mirroring `list_jobs_from_disk`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents_cli.background_agents import BackgroundStatus
from bog_agents_cli.jobs_manager import _JOBS_DIR, PersistentJobsManager

if TYPE_CHECKING:
    import pytest


def _write_job(project_root: Path, task_id: str) -> Path:
    """Write a valid terminal-state job JSON file under the jobs dir.

    Args:
        project_root: Project root passed to `PersistentJobsManager`.
        task_id: Task identifier (also the file stem).

    Returns:
        Path to the written job file.
    """
    jobs_dir = project_root / _JOBS_DIR
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "prompt": "do the thing",
                "label": "demo",
                "status": str(BackgroundStatus.COMPLETED),
                "created_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_unreadable_job_file_does_not_crash_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable job file is skipped instead of crashing ``__init__``."""
    bad_path = _write_job(tmp_path, "bg-bad")

    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == bad_path:
            raise OSError("file is locked")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    # Construction must succeed despite the unreadable file.
    manager = PersistentJobsManager(project_root=tmp_path)

    # The unreadable job is skipped, not loaded.
    assert "bg-bad" not in manager._tasks


def test_readable_sibling_loads_when_one_file_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single unreadable file must not prevent loading other valid jobs."""
    bad_path = _write_job(tmp_path, "bg-bad")
    _write_job(tmp_path, "bg-good")

    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == bad_path:
            raise OSError("file is locked")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    manager = PersistentJobsManager(project_root=tmp_path)

    assert "bg-bad" not in manager._tasks
    assert "bg-good" in manager._tasks
