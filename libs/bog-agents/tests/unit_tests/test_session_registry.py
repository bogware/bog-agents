"""ROADMAP #56: the per-machine session registry."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bog_agents import session_registry as reg


def test_register_heartbeat_and_round_trip(tmp_path: Path) -> None:
    record = reg.register(reg.SessionRecord(name="fix-tests", kind="tui", cwd="/repo", model="anthropic:claude"), registry_dir=tmp_path)
    loaded = reg.load_session(record.session_id, registry_dir=tmp_path)
    assert loaded is not None
    assert loaded.name == "fix-tests" and loaded.pid == os.getpid() and loaded.state == "starting"

    before = loaded.heartbeat
    time.sleep(0.01)
    updated = reg.heartbeat(record.session_id, state="busy", thread_id="t-1", server_url="http://127.0.0.1:2024", registry_dir=tmp_path)
    assert updated is not None
    assert updated.heartbeat > before and updated.state == "busy" and updated.thread_id == "t-1"
    assert reg.load_session(record.session_id, registry_dir=tmp_path).server_url == "http://127.0.0.1:2024"  # type: ignore[union-attr]

    assert reg.unregister(record.session_id, registry_dir=tmp_path)
    assert not reg.unregister(record.session_id, registry_dir=tmp_path)
    assert reg.heartbeat(record.session_id, registry_dir=tmp_path) is None


def test_unknown_keys_are_ignored_and_bad_files_skipped(tmp_path: Path) -> None:
    (tmp_path / "abc.json").write_text('{"session_id": "abc", "name": "x", "future_field": 1}', encoding="utf-8")
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    loaded = reg.load_session("abc", registry_dir=tmp_path)
    assert loaded is not None and loaded.name == "x"
    assert reg.load_session("bad", registry_dir=tmp_path) is None
    assert [r.session_id for r in reg.list_sessions(include_stale=True, registry_dir=tmp_path)] == ["abc"]


def test_stale_records_need_a_live_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = reg.SessionRecord(session_id="old", name="stale", pid=4_000_000, heartbeat=time.time() - 1000)
    fresh = reg.SessionRecord(session_id="new", name="fresh")
    reg.register(old, registry_dir=tmp_path)
    reg.register(fresh, registry_dir=tmp_path)
    monkeypatch.setattr(reg, "pid_alive", lambda pid: pid == os.getpid())

    live = reg.list_sessions(registry_dir=tmp_path)
    assert [r.session_id for r in live] == ["new"]
    assert len(reg.list_sessions(include_stale=True, registry_dir=tmp_path)) == 2

    # A stale heartbeat whose pid is alive (a detached server) still counts.
    monkeypatch.setattr(reg, "pid_alive", lambda pid: True)
    assert {r.session_id for r in reg.list_sessions(registry_dir=tmp_path)} == {"old", "new"}

    monkeypatch.setattr(reg, "pid_alive", lambda pid: pid == os.getpid())
    assert reg.prune_stale(registry_dir=tmp_path) == 1
    assert [r.session_id for r in reg.list_sessions(include_stale=True, registry_dir=tmp_path)] == ["new"]


def test_exited_state_is_never_live(tmp_path: Path) -> None:
    reg.register(reg.SessionRecord(session_id="gone", state="exited"), registry_dir=tmp_path)
    assert reg.list_sessions(registry_dir=tmp_path) == []


def test_find_session_by_id_name_or_unique_prefix(tmp_path: Path) -> None:
    reg.register(reg.SessionRecord(session_id="aaa111", name="review-pr"), registry_dir=tmp_path)
    reg.register(reg.SessionRecord(session_id="bbb222", name="refactor"), registry_dir=tmp_path)
    assert reg.find_session("aaa111", registry_dir=tmp_path).name == "review-pr"
    assert reg.find_session("refactor", registry_dir=tmp_path).session_id == "bbb222"
    assert reg.find_session("rev", registry_dir=tmp_path).session_id == "aaa111"
    with pytest.raises(LookupError, match="matches several"):
        reg.find_session("re", registry_dir=tmp_path)
    with pytest.raises(LookupError, match="no live session"):
        reg.find_session("zzz", registry_dir=tmp_path)


def test_pid_alive_for_self_and_absent() -> None:
    assert reg.pid_alive(os.getpid())
    assert not reg.pid_alive(0)
    assert not reg.pid_alive(-5)


def test_format_sessions_table(tmp_path: Path) -> None:
    now = time.time()
    rows = [
        reg.SessionRecord(session_id="aaa111", name="review-pr", kind="tui", state="busy", model="anthropic:claude", cwd="/repo", heartbeat=now - 5),
        reg.SessionRecord(session_id="bbb222", kind="daemon", state="idle", heartbeat=now - 3600 * 3),
    ]
    text = reg.format_sessions(rows, now=now)
    lines = text.splitlines()
    assert lines[0].startswith("SESSION") and "CWD" in lines[0]
    assert "review-pr" in lines[1] and "busy" in lines[1] and "5s" in lines[1]
    assert "bbb222" in lines[2] and "3h" in lines[2]
    assert reg.format_sessions([]) == "No live sessions on this machine."
