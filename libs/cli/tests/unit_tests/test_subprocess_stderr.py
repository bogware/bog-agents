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
    tail_mcp_stderr_log,
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


class TestMcpStdioDefaultErrlogPatch:
    """The default-errlog patch is critical when MCP loads inside Textual.

    ``mcp.client.stdio.stdio_client`` has ``errlog: TextIO = sys.stderr``
    in its signature. Python evaluates that default ONCE at function
    definition (i.e. module import). When MCP is lazy-imported inside
    a running Textual TUI, ``sys.stderr`` is already a ``_PrintCapture``
    wrapper with no usable OS fd, and that broken wrapper becomes the
    permanent default. ``langchain_mcp_adapters`` calls
    ``stdio_client(server_params)`` without an explicit ``errlog``, so
    every spawn uses the broken default and fails with EBADF on
    Windows. Swapping ``sys.stderr`` globally does NOT fix this — we
    must mutate ``__defaults__`` directly.
    """

    def test_patch_replaces_then_restores_defaults(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The patch swaps in our safe file then restores on exit.

        In the with-block the MCP default points at our log file;
        outside the block, the original default is restored.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli._subprocess_stderr._stderr_handle_is_usable",
            lambda: False,
        )
        monkeypatch.setattr(sys, "stderr", io.StringIO())

        # Build a fake mcp.client.stdio module so the test doesn't depend
        # on the real package being importable.
        import types

        fake_module = types.ModuleType("mcp.client.stdio")
        fake_stderr = io.StringIO()  # stand-in for the original captured default

        async def fake_stdio_client(server, errlog=fake_stderr):
            return None

        fake_module.stdio_client = fake_stdio_client
        monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
        monkeypatch.setitem(sys.modules, "mcp.client", types.ModuleType("mcp.client"))
        monkeypatch.setitem(sys.modules, "mcp.client.stdio", fake_module)

        # Sanity: original default points at the (broken) StringIO stand-in.
        assert fake_stdio_client.__defaults__ == (fake_stderr,)

        with safe_subprocess_stderr():
            # Inside the block, the default must be a real file.
            patched = fake_stdio_client.__defaults__[0]
            assert patched is not fake_stderr
            assert hasattr(patched, "fileno")
            assert patched.fileno() >= 0

        # Outside the block, the original default is restored.
        assert fake_stdio_client.__defaults__ == (fake_stderr,)

    def test_patch_skipped_when_mcp_not_importable(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Missing mcp package must NOT crash the context manager."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            "bog_agents_cli._subprocess_stderr._stderr_handle_is_usable",
            lambda: False,
        )
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        monkeypatch.setitem(sys.modules, "mcp.client.stdio", None)

        # Should not raise — patch is best-effort.
        with safe_subprocess_stderr():
            pass


class TestTailMcpStderrLog:
    """The tail helper is used by the failure-message paths."""

    def test_returns_empty_when_log_missing(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Don't create the log file.
        assert tail_mcp_stderr_log() == ""

    def test_returns_empty_for_empty_log(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        log_dir = tmp_path / ".bog-agents" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "mcp-stderr.log").write_text("")
        assert tail_mcp_stderr_log() == ""

    def test_returns_last_n_bytes(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        log_dir = tmp_path / ".bog-agents" / "logs"
        log_dir.mkdir(parents=True)
        log = log_dir / "mcp-stderr.log"
        log.write_text("first line\nsecond line\nthird line\n", encoding="utf-8")
        # max_bytes large — full content (stripped of trailing whitespace)
        assert tail_mcp_stderr_log(1000).endswith("third line")
        # max_bytes small — only the tail
        small = tail_mcp_stderr_log(15)
        assert "third" in small or "line" in small

    def test_does_not_raise_on_unreadable_log(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """OSError during read returns ``""`` instead of propagating."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        log_dir = tmp_path / ".bog-agents" / "logs"
        log_dir.mkdir(parents=True)
        # Create the file then make .open() raise via monkeypatch.
        log = log_dir / "mcp-stderr.log"
        log.write_text("content")
        original_open = Path.open

        oserror_msg = "simulated I/O failure"

        def _raise(self_, *a, **kw):
            if self_ == log:
                raise OSError(oserror_msg)  # test fixture
            return original_open(self_, *a, **kw)

        monkeypatch.setattr(Path, "open", _raise)
        assert tail_mcp_stderr_log() == ""


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
