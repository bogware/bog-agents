"""Shared fixtures for daemon unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_daemon_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all daemon file-system paths to a temporary directory."""
    import bog_agents_daemon.store as store_mod

    daemon_dir = tmp_path / ".bog-agents" / "daemon"
    daemon_dir.mkdir(parents=True)
    runs_dir = daemon_dir / "runs"
    runs_dir.mkdir()

    monkeypatch.setattr(store_mod, "_DAEMON_DIR", daemon_dir)
    monkeypatch.setattr(store_mod, "_JOBS_FILE", daemon_dir / "jobs.json")
    monkeypatch.setattr(store_mod, "_RUNS_DIR", runs_dir)
    return daemon_dir
