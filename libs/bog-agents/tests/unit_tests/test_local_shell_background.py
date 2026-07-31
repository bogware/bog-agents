"""Tests for LocalShellBackend background execution + auto-background (Tier-1 #1)."""

from __future__ import annotations

import sys
from pathlib import Path

from bog_agents.backends.local_shell import LocalShellBackend

PY = sys.executable


class TestExplicitBackground:
    def test_background_returns_task_id_immediately(self, tmp_path: Path) -> None:
        be = LocalShellBackend(root_dir=str(tmp_path), inherit_env=True)
        try:
            resp = be.execute(f'{PY} -c "import time; time.sleep(20)"', background=True)
            assert resp.exit_code == 0
            assert "background as task" in resp.output
            running = be.list_background()
            assert len(running) == 1
            assert running[0].running is True
        finally:
            be.close()

    def test_poll_and_kill_background(self, tmp_path: Path) -> None:
        be = LocalShellBackend(root_dir=str(tmp_path), inherit_env=True)
        try:
            resp = be.execute(f'{PY} -c "import time; time.sleep(20)"', background=True)
            # extract the task id from the message
            tid = resp.output.split("task ", 1)[1].split(".", 1)[0].strip()
            assert be.poll_background(tid).running is True
            assert be.kill_background(tid) is True
            final = be.wait_background([tid], mode="all", timeout=10)[0]
            assert final.running is False
        finally:
            be.close()


class TestAutoBackground:
    def test_fast_command_returns_foreground_result(self, tmp_path: Path) -> None:
        be = LocalShellBackend(root_dir=str(tmp_path), inherit_env=True, auto_background_after=5.0)
        try:
            resp = be.execute(f"{PY} -c \"print('hello-fg')\"")
            assert resp.exit_code == 0
            assert "hello-fg" in resp.output
            # finished within budget → not left in the registry
            assert be.list_background() == []
        finally:
            be.close()

    def test_slow_command_is_backgrounded_not_killed(self, tmp_path: Path) -> None:
        be = LocalShellBackend(root_dir=str(tmp_path), inherit_env=True, auto_background_after=0.5)
        try:
            resp = be.execute(f'{PY} -c "import time; time.sleep(20)"')
            assert "moved to the background" in resp.output
            assert resp.exit_code == 0
            # still alive in the registry
            running = [r for r in be.list_background() if r.running]
            assert len(running) == 1
        finally:
            be.close()

    def test_nonzero_exit_preserved_in_foreground_path(self, tmp_path: Path) -> None:
        be = LocalShellBackend(root_dir=str(tmp_path), inherit_env=True, auto_background_after=5.0)
        try:
            resp = be.execute(f'{PY} -c "import sys; sys.exit(3)"')
            assert resp.exit_code == 3
            assert "Exit code: 3" in resp.output
        finally:
            be.close()

    def test_default_backend_still_kills_on_timeout(self, tmp_path: Path) -> None:
        # No auto_background_after → classic behaviour: a per-call timeout kills.
        be = LocalShellBackend(root_dir=str(tmp_path), inherit_env=True)
        try:
            resp = be.execute(f'{PY} -c "import time; time.sleep(20)"', timeout=1)
            assert resp.exit_code == 124  # timeout exit code, killed
            assert be.list_background() == []  # nothing left running
        finally:
            be.close()
