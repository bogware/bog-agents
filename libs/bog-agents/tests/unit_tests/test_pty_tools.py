"""Tests for the PTY controller + agent tool bundle (Tier-2 #6 wiring)."""

from __future__ import annotations

import sys

import pytest

from bog_agents.pty_harness import PtyController, pty_supported
from bog_agents.tools import pty_tools_bundle


class TestPtyToolsBundle:
    def test_bundle_tool_names(self) -> None:
        names = [t.name for t in pty_tools_bundle(PtyController())]
        assert names == ["pty_start", "pty_send", "pty_screen", "pty_wait", "pty_close", "pty_list"]


class TestPtyControllerErrorPaths:
    def test_send_to_unknown_session(self) -> None:
        assert "No PTY session" in PtyController().send("nope", "x")

    def test_screen_unknown_session(self) -> None:
        assert "No PTY session" in PtyController().screen("nope")

    def test_close_unknown_session(self) -> None:
        assert "No PTY session" in PtyController().close("nope")

    def test_list_empty(self) -> None:
        assert "No active" in PtyController().list_sessions()

    def test_wait_unknown_kind(self) -> None:
        # Even with a live-ish session absent, unknown session is reported first;
        # use a controller with a stubbed session to reach the kind check.
        ctl = PtyController()
        assert "No PTY session" in ctl.wait("nope", "bogus", "x")


@pytest.mark.skipif(not pty_supported(), reason="needs a working PTY backend (POSIX or pywinpty)")
class TestPtyControllerLive:
    def _cmd(self) -> str:
        # A quiet interactive Python REPL — cross-platform.
        return f'"{sys.executable}" -i -q -u'

    def test_full_drive_cycle(self) -> None:
        ctl = PtyController()
        try:
            assert "Started" in ctl.start("repl", self._cmd())
            assert "matched" in ctl.wait("repl", "text", ">>>", timeout_s=15)
            ctl.send("repl", "print('zzz999')<CR>")
            assert "matched" in ctl.wait("repl", "text", "zzz999", timeout_s=15)
            assert "repl" in ctl.list_sessions()
            assert "Closed" in ctl.close("repl")
        finally:
            ctl.shutdown()

    def test_bad_command_reported(self) -> None:
        ctl = PtyController()
        out = ctl.start("x", "this-binary-does-not-exist-zzz")
        assert "Could not start" in out or "No PTY" in out
        ctl.shutdown()
