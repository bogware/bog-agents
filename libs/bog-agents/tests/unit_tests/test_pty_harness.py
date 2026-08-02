"""Tests for the PTY harness (Tier-2 #6).

The pure layers (key encoding, output model, wait conditions) run everywhere;
the live `PtySession` is POSIX-only and skipped on Windows.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import bog_agents.pty_harness as _pty_harness_module
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

# Absolute path to the module under test, for the subprocess-isolated check.
PTY_HARNESS_PATH = Path(_pty_harness_module.__file__)


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
        # Real PTYs emit CRLF; bare LF would staircase under a real terminal grid.
        out.feed(b"a\r\nb\r\nc\r\nd\r\n")
        assert out.snapshot(tail_lines=2).splitlines() == ["c", "d"]


class TestStripAnsi:
    def test_removes_color_and_cursor_moves(self) -> None:
        assert strip_ansi("\x1b[2J\x1b[H\x1b[32mok\x1b[0m") == "ok"


def _pyte_available() -> bool:
    try:
        import pyte  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _pyte_available(), reason="requires pyte")
class TestPyteGrid:
    def test_grid_collapses_cursor_redraws(self) -> None:
        out = TerminalOutput(cols=20, rows=3)
        # Move cursor right then write — a naive line buffer keeps both; the grid
        # renders the final screen position.
        out.feed(b"a\x1b[2Cb\r\nsecond")
        grid = out.grid()
        assert grid is not None
        assert grid.splitlines()[0] == "a  b"
        assert "second" in grid

    def test_snapshot_uses_grid_when_available(self) -> None:
        out = TerminalOutput(cols=20, rows=3)
        out.feed(b"hello world\r\n")
        assert "hello world" in out.snapshot()

    def test_wait_still_matches_line_buffer(self) -> None:
        # Even with pyte on, .text (used by wait conditions) keeps all output.
        out = TerminalOutput(cols=10, rows=2)
        out.feed(b"\x1b[31mMARKER\x1b[0m\r\n")
        assert "MARKER" in out.text


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


@pytest.mark.skipif(not pty_supported(), reason="PtySession requires a POSIX PTY")
class TestFailedExecDoesNotForkADuplicate:
    """`pty.fork()` hands the child a full copy of this process.

    If a failed `execvpe` propagates as a normal exception, the child unwinds
    back into the caller's Python and keeps running the whole program a second
    time. Under pytest-xdist that is a duplicate worker reporting duplicate
    results (crashing the scheduler with `mark_test_complete` ValueError); in a
    live agent it is two processes sharing stdio. The child must always exit.
    """

    def test_spawn_raises_instead_of_returning_in_the_child(self) -> None:
        from bog_agents.pty_harness import _PosixPtyBackend

        backend = _PosixPtyBackend()
        with pytest.raises(OSError, match="could not run"):
            backend.spawn(["this-binary-does-not-exist-zzz"], None, None)

    def test_bad_cwd_also_raises(self) -> None:
        from bog_agents.pty_harness import _PosixPtyBackend

        backend = _PosixPtyBackend()
        with pytest.raises(OSError, match="could not run"):
            backend.spawn(["/bin/echo", "hi"], None, "/definitely/not/a/real/dir/zzz")

    def test_no_second_process_survives_a_failed_exec(self, tmp_path: Path) -> None:
        # Run in a child interpreter: if this regressed, the duplicate process
        # would otherwise go on to corrupt *this* pytest session.
        marker = tmp_path / "reached.txt"
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('ph', {str(PTY_HARNESS_PATH)!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules['ph'] = mod\n"
            "spec.loader.exec_module(mod)\n"
            "ctl = mod.PtyController()\n"
            "ctl.start('x', 'this-binary-does-not-exist-zzz')\n"
            # Every process that gets here appends one line.
            f"open({str(marker)!r}, 'a').write('reached\n')\n"
            "ctl.shutdown()\n"
            "import time; time.sleep(0.3)\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1, f"a failed exec forked a duplicate process: {lines}"
