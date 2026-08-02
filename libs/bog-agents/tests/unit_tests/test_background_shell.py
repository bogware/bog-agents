"""Tests for the background shell command registry (Tier-1 #1)."""

from __future__ import annotations

import sys
import time

from bog_agents.backends.background_shell import BackgroundShellManager

PY = sys.executable


def _emit(n: int, delay: float = 0.05) -> str:
    """A portable single-line command that prints 0..n-1 (flushed) with a delay."""
    return f'{PY} -c "import time; [print(i, flush=True) or time.sleep({delay}) for i in range({n})]"'


class TestBackgroundShellManager:
    def test_start_poll_and_finish(self) -> None:
        mgr = BackgroundShellManager()
        try:
            tid = mgr.start(_emit(4, 0.05))
            snap = mgr.poll(tid)
            assert snap is not None and snap.running is True  # still going right after start
            # Wait for completion.
            final = mgr.wait([tid], mode="all", timeout=10)[0]
            assert final.running is False
            assert final.exit_code == 0
            assert "0" in final.output and "3" in final.output
        finally:
            mgr.close()

    def test_kill_stops_a_long_command(self) -> None:
        mgr = BackgroundShellManager()
        try:
            tid = mgr.start(f'{PY} -c "import time; time.sleep(30)"')
            assert mgr.poll(tid).running is True
            assert mgr.kill(tid) is True
            final = mgr.wait([tid], mode="all", timeout=10)[0]
            assert final.running is False
            assert final.exit_code is not None  # process actually reaped
        finally:
            mgr.close()

    def test_wait_any_returns_on_first_exit(self) -> None:
        mgr = BackgroundShellManager()
        try:
            fast = mgr.start(f'{PY} -c "print(1)"')
            slow = mgr.start(f'{PY} -c "import time; time.sleep(20)"')
            snaps = mgr.wait([fast, slow], mode="any", timeout=10)
            by_id = {s.task_id: s for s in snaps}
            assert by_id[fast].running is False  # the fast one finished
        finally:
            mgr.close()

    def test_unknown_task_id_polls_none(self) -> None:
        mgr = BackgroundShellManager()
        assert mgr.poll("nope") is None
        assert mgr.kill("nope") is False

    def test_list_reports_all(self) -> None:
        mgr = BackgroundShellManager()
        try:
            a = mgr.start(f'{PY} -c "print(1)"')
            b = mgr.start(f'{PY} -c "import time; time.sleep(20)"')
            time.sleep(0.3)
            ids = {r.task_id for r in mgr.list()}
            assert {a, b} <= ids
        finally:
            mgr.close()

    def test_output_buffer_is_capped_to_tail(self) -> None:
        mgr = BackgroundShellManager(max_output_bytes=200)
        try:
            # Emit far more than 200 bytes; the tail must be kept.
            tid = mgr.start(f"{PY} -c \"print('x'*5000); print('TAILMARKER')\"")
            final = mgr.wait([tid], mode="all", timeout=10)[0]
            assert final.truncated is True
            assert len(final.output) <= 400  # bounded (buffer + decode slack)
            assert "TAILMARKER" in final.output  # newest output survived
        finally:
            mgr.close()

    def test_close_kills_running_commands(self) -> None:
        mgr = BackgroundShellManager()
        tid = mgr.start(f'{PY} -c "import time; time.sleep(30)"')
        assert mgr.poll(tid).running is True
        mgr.close()
        # After close the registry is cleared.
        assert mgr.poll(tid) is None
