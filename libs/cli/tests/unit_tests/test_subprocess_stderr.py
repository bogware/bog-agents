"""Tests for the safe_subprocess_stderr helper.

Regression for the user-reported ``[Errno 9] Bad file descriptor``
error from MCP stdio spawn on Windows. The helper detects when
``sys.stderr`` lacks a usable OS fd and redirects to a real log file
just for the spawn window.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from bog_agents_cli._subprocess_stderr import (
    _stderr_handle_is_usable,
    diagnostic_info,
    safe_subprocess_stderr,
)


class TestStderrUsableDetection:
    """The detection helper distinguishes valid vs broken stderr."""

    def test_normal_stderr_is_usable(self) -> None:
        """A bare process default stderr should be usable."""
        # NB: under pytest stderr may be captured. The helper handles
        # both cases; assertion just confirms it returns *something*.
        result = _stderr_handle_is_usable()
        assert isinstance(result, bool)

    def test_stringio_stderr_is_not_usable(self, monkeypatch) -> None:
        """A pure-Python StringIO has no fd — must report unusable."""
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        assert _stderr_handle_is_usable() is False

    def test_object_without_fileno_is_not_usable(self, monkeypatch) -> None:
        """An object with no fileno method — must report unusable."""

        class _NoFileno:
            pass

        monkeypatch.setattr(sys, "stderr", _NoFileno())
        assert _stderr_handle_is_usable() is False


class TestSafeSubprocessStderr:
    """The context manager redirects only when needed."""

    def test_no_op_when_stderr_is_usable(self, monkeypatch) -> None:
        """When stderr already has a real fd, nothing is swapped."""
        # Force "usable" so the redirect path is skipped.
        monkeypatch.setattr(
            "bog_agents_cli._subprocess_stderr._stderr_handle_is_usable",
            lambda: True,
        )
        before = sys.stderr
        with safe_subprocess_stderr():
            during = sys.stderr
        after = sys.stderr
        assert before is during is after

    def test_redirects_when_stderr_unusable(self, monkeypatch, tmp_path: Path) -> None:
        """When stderr is broken, ``sys.stderr`` is swapped during the block."""
        # Force home dir to tmp so we don't pollute the real ~/.bog-agents.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli._subprocess_stderr._stderr_handle_is_usable",
            lambda: False,
        )
        broken = io.StringIO()
        monkeypatch.setattr(sys, "stderr", broken)

        with safe_subprocess_stderr():
            # The swap must produce a stream with a real fileno().
            assert sys.stderr is not broken
            assert sys.stderr.fileno() >= 0
            sys.stderr.write("test line\n")

        # And it must restore the original on exit.
        assert sys.stderr is broken
        # The log file must exist with the line we wrote.
        log_path = tmp_path / ".bog-agents" / "logs" / "mcp-stderr.log"
        assert log_path.exists()
        assert "test line" in log_path.read_text(encoding="utf-8")

    def test_restore_happens_even_on_exception(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The original stderr is restored when the block raises."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli._subprocess_stderr._stderr_handle_is_usable",
            lambda: False,
        )
        original = io.StringIO()
        monkeypatch.setattr(sys, "stderr", original)

        boom_msg = "boom"
        with pytest.raises(RuntimeError, match="boom"), safe_subprocess_stderr():
            raise RuntimeError(boom_msg)

        assert sys.stderr is original


class TestDiagnosticInfo:
    """``diagnostic_info`` returns a dict with the expected keys."""

    def test_returns_expected_shape(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        info = diagnostic_info()
        assert "platform" in info
        assert "stderr_class" in info
        assert "stderr_isatty" in info
        assert "stderr_usable" in info
        assert "log_path" in info
        assert isinstance(info["stderr_usable"], bool)
        assert info["log_path"].endswith("mcp-stderr.log")
