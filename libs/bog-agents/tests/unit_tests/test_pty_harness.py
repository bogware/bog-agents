"""Tests for the PTY harness (Tier-2 #6).

The pure layers (key encoding, output model, wait conditions) run everywhere;
the live `PtySession` is POSIX-only and skipped on Windows.
"""

from __future__ import annotations

import pytest

from bog_agents.pty_harness import (
    KeyEncodeError,
    PtySession,
    TerminalOutput,
    WaitContext,
    WaitGone,
    WaitRegex,
    WaitStable,
    WaitText,
    _winpty_available,
    encode_keys,
    pty_supported,
    strip_ansi,
)


class TestEncodeKeys:
    def test_literal_text(self) -> None:
        assert encode_keys("hello") == b"hello"

    def test_named_keys(self) -> None:
        assert encode_keys("<CR>") == b"\r"
        assert encode_keys("<Esc>") == b"\x1b"
        assert encode_keys("<Tab>") == b"\t"
        assert encode_keys("<Up>") == b"\x1b[A"
        assert encode_keys("<F5>") == b"\x1b[15~"

    def test_mixed_literal_and_named(self) -> None:
        assert encode_keys("<Esc>:wq<CR>") == b"\x1b:wq\r"

    def test_ctrl_modifier(self) -> None:
        assert encode_keys("<C-c>") == b"\x03"  # Ctrl-C
        assert encode_keys("<C-a>") == b"\x01"

    def test_alt_modifier_prefixes_esc(self) -> None:
        assert encode_keys("<M-x>") == b"\x1bx"
        assert encode_keys("<A-b>") == b"\x1bb"

    def test_shift_uppercases_letter(self) -> None:
        assert encode_keys("<S-a>") == b"A"

    def test_repeated_keys(self) -> None:
        assert encode_keys("<Up><Up><CR>") == b"\x1b[A\x1b[A\r"

    def test_unknown_token_raises(self) -> None:
        with pytest.raises(KeyEncodeError):
            encode_keys("<Nope>")


class TestTerminalOutput:
    def test_feed_and_text(self) -> None:
        out = TerminalOutput()
        out.feed(b"hello\r\nworld\r\n")
        assert "hello" in out.text
        assert out.lines[-1] == "world" or out.lines[-2] == "world"

    def test_ansi_stripped(self) -> None:
        out = TerminalOutput()
        out.feed(b"\x1b[31mRED\x1b[0m done")
        assert "RED done" in out.text
        assert "\x1b[" not in out.text

    def test_snapshot_tail_lines(self) -> None:
        out = TerminalOutput()
        out.feed(b"a\nb\nc\nd\n")
        assert out.snapshot(tail_lines=2).splitlines() == ["c", "d"]


class TestStripAnsi:
    def test_removes_color_and_cursor_moves(self) -> None:
        assert strip_ansi("\x1b[2J\x1b[H\x1b[32mok\x1b[0m") == "ok"


class TestWaitConditions:
    def _ctx(self, screen: str = "", ms: float = 0.0) -> WaitContext:
        return WaitContext(screen=screen, ms_since_change=ms)

    def test_wait_text(self) -> None:
        assert WaitText("READY").satisfied(self._ctx("... READY ...")) is True
        assert WaitText("READY").satisfied(self._ctx("nope")) is False

    def test_wait_regex(self) -> None:
        assert WaitRegex(r"\d+ passed").satisfied(self._ctx("12 passed")) is True
        assert WaitRegex(r"\d+ passed").satisfied(self._ctx("no numbers")) is False

    def test_wait_gone(self) -> None:
        assert WaitGone("Loading").satisfied(self._ctx("done")) is True
        assert WaitGone("Loading").satisfied(self._ctx("Loading...")) is False

    def test_wait_stable(self) -> None:
        assert WaitStable(quiet_ms=200).satisfied(self._ctx("x", ms=250)) is True
        assert WaitStable(quiet_ms=200).satisfied(self._ctx("x", ms=100)) is False


@pytest.mark.skipif(not pty_supported(), reason="PtySession requires a POSIX PTY")
class TestPtySessionPosix:
    def test_construction_ok_on_posix(self) -> None:
        PtySession(command=["true"])  # does not raise

    def test_drive_a_program(self) -> None:
        # `cat` echoes stdin back through the PTY.
        session = PtySession(command=["sh", "-c", "echo READY; cat"])
        session.start()
        try:
            assert session.wait(WaitText("READY"), timeout_s=5).ok is True
            session.send("ping<CR>")
            assert session.wait(WaitText("ping"), timeout_s=5).ok is True
        finally:
            session.close()

    def test_wait_times_out_when_unmet(self) -> None:
        session = PtySession(command=["sh", "-c", "echo hi; cat"])
        session.start()
        try:
            result = session.wait(WaitText("NEVER"), timeout_s=0.5)
            assert result.ok is False
        finally:
            session.close()


@pytest.mark.skipif(not _winpty_available(), reason="requires pywinpty (Windows ConPTY)")
class TestPtySessionWindows:
    def test_drive_python_repl(self) -> None:
        import sys

        session = PtySession(command=[sys.executable, "-i", "-q", "-u"])
        session.start()
        try:
            assert session.wait(WaitText(">>>"), timeout_s=15).ok is True
            session.send("print('pong123')<CR>")
            assert session.wait(WaitText("pong123"), timeout_s=15).ok is True
        finally:
            session.close()


@pytest.mark.skipif(pty_supported(), reason="tests the fail-closed path when no PTY backend exists")
def test_pty_session_fails_closed_when_unsupported() -> None:
    with pytest.raises(RuntimeError, match=r"pywinpty|pty"):
        PtySession(command=["cmd"])
