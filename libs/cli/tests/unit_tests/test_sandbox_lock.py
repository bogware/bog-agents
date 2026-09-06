"""ROADMAP #76: sandbox snapshot templates in `.bog-agents/sandbox.lock`."""

from __future__ import annotations

from pathlib import Path

from bog_agents_cli import sandbox_lock as sl


def test_record_validate_stale_forget(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("a", encoding="utf-8")
    assert sl.snapshot_for(tmp_path, "daytona") == (None, "no daytona snapshot recorded")
    assert "No sandbox snapshots" in sl.describe(tmp_path)
    path = sl.record_snapshot(tmp_path, "daytona", "snap-123", note="deps installed")
    assert path == tmp_path / ".bog-agents" / "sandbox.lock"
    assert sl.read_lock(tmp_path)["snapshots"]["daytona"]["lock_hashes"] == sl.current_hashes(tmp_path)
    assert sl.snapshot_for(tmp_path, "daytona")[0] == "snap-123" and "valid" in sl.describe(tmp_path)

    (tmp_path / "uv.lock").write_text("b", encoding="utf-8")
    snapshot, reason = sl.snapshot_for(tmp_path, "daytona")
    assert snapshot is None and "stale" in reason and "uv.lock" in reason and "STALE" in sl.describe(tmp_path)
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert "package-lock.json" in sl.snapshot_for(tmp_path, "daytona")[1]

    assert sl.forget_snapshot(tmp_path, "daytona") and not sl.forget_snapshot(tmp_path, "daytona")
    path.write_text("not json", encoding="utf-8")
    assert sl.read_lock(tmp_path) == {"version": 1, "snapshots": {}}
