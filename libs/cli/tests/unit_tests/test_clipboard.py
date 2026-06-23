"""Tests for clipboard helpers."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bog_agents_cli import clipboard as clipboard_module


class TestClipboardReadTimeout:
    """Clipboard READ helpers must bound their subprocess with a timeout.

    A slow/hung clipboard helper would otherwise block the (synchronous) read
    indefinitely; the read is invoked from the TUI, so an unbounded subprocess
    can freeze the whole event loop. ``subprocess.TimeoutExpired`` is a subclass
    of ``SubprocessError``, which ``read_clipboard_text`` already catches.
    """

    def test_read_windows_clipboard_passes_timeout(self) -> None:
        with patch.object(clipboard_module.subprocess, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="clip")
            clipboard_module._read_windows_clipboard()

        assert mock_run.call_args.kwargs.get("timeout") == 5

    def test_read_command_output_passes_timeout(self) -> None:
        with patch.object(clipboard_module.subprocess, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="clip")
            clipboard_module._read_command_output(["pbpaste"])

        assert mock_run.call_args.kwargs.get("timeout") == 5

    def test_copy_windows_clip_passes_timeout(self) -> None:
        with patch.object(clipboard_module.subprocess, "run") as mock_run:
            clipboard_module._copy_windows_clip("text")

        assert mock_run.call_args.kwargs.get("timeout") == 5

    def test_read_returns_none_on_timeout(self) -> None:
        mock_pyperclip = MagicMock()
        mock_pyperclip.paste.side_effect = RuntimeError("clipboard unavailable")

        with (
            patch.object(clipboard_module.sys, "platform", "win32"),
            patch.dict(sys.modules, {"pyperclip": mock_pyperclip}),
            patch.object(
                clipboard_module,
                "_read_windows_clipboard",
                side_effect=clipboard_module.subprocess.TimeoutExpired(
                    cmd="powershell.exe", timeout=5
                ),
            ),
        ):
            result = clipboard_module.read_clipboard_text()

        assert result is None


def test_read_clipboard_text_falls_back_to_windows_clipboard() -> None:
    """Windows clipboard fallback should be used when pyperclip fails."""
    mock_pyperclip = MagicMock()
    mock_pyperclip.paste.side_effect = RuntimeError("clipboard unavailable")

    with (
        patch.object(clipboard_module.sys, "platform", "win32"),
        patch.dict(sys.modules, {"pyperclip": mock_pyperclip}),
        patch.object(
            clipboard_module,
            "_read_windows_clipboard",
            return_value="hello from windows",
        ) as mock_read,
    ):
        result = clipboard_module.read_clipboard_text()

    assert result == "hello from windows"
    mock_read.assert_called_once_with()


def test_copy_selection_to_clipboard_uses_windows_fallback() -> None:
    """Selection copy should fall back to the native Windows clipboard helper."""
    widget = MagicMock()
    widget.text_selection = SimpleNamespace(end=(0, 3))
    widget.get_selection.return_value = ("copied text", None)

    app = MagicMock()
    app.query.return_value = [widget]

    mock_pyperclip = MagicMock()
    mock_pyperclip.copy.side_effect = RuntimeError("pyperclip unavailable")

    with (
        patch.object(clipboard_module.sys, "platform", "win32"),
        patch.dict(sys.modules, {"pyperclip": mock_pyperclip}),
        patch.object(clipboard_module, "_copy_windows_clip") as mock_windows_copy,
    ):
        copied = clipboard_module.copy_selection_to_clipboard(app)

    assert copied is True
    mock_windows_copy.assert_called_once_with("copied text")
    app.notify.assert_called_once()


def _setup_copy(text: str) -> tuple[MagicMock, MagicMock]:
    """Helper: build the (widget, app) pair for a single-selection copy."""
    widget = MagicMock()
    widget.text_selection = SimpleNamespace(end=(0, len(text)))
    widget.get_selection.return_value = (text, None)
    app = MagicMock()
    app.query.return_value = [widget]
    return widget, app


class TestCopyNotificationFormat:
    """Regression tests for the simplified copy notification.

    Pre-fix: ``copy_selection_to_clipboard`` notified
    ``f'"{first 40 chars}..." copied'`` — a content preview that
    visually collided with the click-to-show-timestamp toast (showing
    the date) and produced a noisy double-popup. Post-fix: short
    char-count message; the timestamp toast carries the date when
    relevant.
    """

    @staticmethod
    def _capture_notify(text: str) -> str:
        _, app = _setup_copy(text)
        # Isolate the notification format from the clipboard backend so the
        # test never touches a real clipboard.
        with patch.object(
            clipboard_module, "_write_to_clipboard", return_value=(True, None)
        ):
            clipboard_module.copy_selection_to_clipboard(app)
        # First positional arg of notify(...) — the message.
        return app.notify.call_args.args[0]

    def test_short_text_uses_compact_form(self) -> None:
        msg = self._capture_notify("hello world")
        assert msg == "Copied! (11 chars)"

    def test_long_text_uses_truncated_form(self) -> None:
        # _PREVIEW_MAX_LENGTH = 40 in the module; pick something past it.
        long_text = "x" * 100
        msg = self._capture_notify(long_text)
        assert msg.startswith("[Copied text truncated] copied!")
        assert "100 chars" in msg

    def test_notification_does_not_echo_content(self) -> None:
        """The content must NOT appear in the notification."""
        secret = "alpha bravo charlie"
        msg = self._capture_notify(secret)
        assert secret not in msg

    def test_multi_selection_notification(self) -> None:
        widget_a = MagicMock()
        widget_a.text_selection = SimpleNamespace(end=(0, 5))
        widget_a.get_selection.return_value = ("first", None)
        widget_b = MagicMock()
        widget_b.text_selection = SimpleNamespace(end=(0, 6))
        widget_b.get_selection.return_value = ("second", None)
        app = MagicMock()
        app.query.return_value = [widget_a, widget_b]

        with patch.object(
            clipboard_module, "_write_to_clipboard", return_value=(True, None)
        ):
            clipboard_module.copy_selection_to_clipboard(app)

        msg = app.notify.call_args.args[0]
        assert "2 selections" in msg
        assert "first" not in msg
        assert "second" not in msg


class TestWriteToClipboardOrdering:
    """The clipboard backend order is load-bearing for the lag fix.

    pyperclip (native Win32 ``CF_UNICODETEXT``, no subprocess) must be tried
    before ``clip.exe``. The old order spawned ``clip.exe`` first on every
    copy, which blocked the event loop and mangled non-ASCII text.
    """

    def test_prefers_pyperclip_over_clip_exe(self) -> None:
        mock_pyperclip = MagicMock()

        with (
            patch.object(clipboard_module.sys, "platform", "win32"),
            patch.dict(sys.modules, {"pyperclip": mock_pyperclip}),
            patch.object(clipboard_module, "_copy_windows_clip") as mock_clip,
        ):
            success, error = clipboard_module._write_to_clipboard("hello")

        assert success is True
        assert error is None
        mock_pyperclip.copy.assert_called_once_with("hello")
        # clip.exe must NOT run when pyperclip succeeds — that is the whole
        # point of the reorder (no subprocess spawn on the hot path).
        mock_clip.assert_not_called()

    def test_falls_back_to_clip_exe_when_pyperclip_fails(self) -> None:
        mock_pyperclip = MagicMock()
        mock_pyperclip.copy.side_effect = RuntimeError("pyperclip backend missing")

        with (
            patch.object(clipboard_module.sys, "platform", "win32"),
            patch.dict(sys.modules, {"pyperclip": mock_pyperclip}),
            patch.object(clipboard_module, "_copy_windows_clip") as mock_clip,
        ):
            success, _error = clipboard_module._write_to_clipboard("hello")

        assert success is True
        mock_pyperclip.copy.assert_called_once_with("hello")
        mock_clip.assert_called_once_with("hello")

    def test_reports_failure_when_all_methods_fail(self) -> None:
        mock_pyperclip = MagicMock()
        mock_pyperclip.copy.side_effect = RuntimeError("no backend")

        with (
            patch.object(clipboard_module.sys, "platform", "win32"),
            patch.dict(sys.modules, {"pyperclip": mock_pyperclip}),
            patch.object(
                clipboard_module,
                "_copy_windows_clip",
                side_effect=OSError("clip.exe missing"),
            ),
        ):
            success, error = clipboard_module._write_to_clipboard("hello")

        assert success is False
        assert isinstance(error, OSError)


class TestAsyncCopyDispatch:
    """`copy_selection_to_clipboard_async` keeps the event loop unblocked.

    The selection is gathered synchronously (UI thread), but the blocking
    clipboard write + the result toast are pushed onto a thread worker.
    """

    def test_dispatches_write_to_thread_worker(self) -> None:
        _, app = _setup_copy("payload")

        with patch.object(
            clipboard_module, "_write_to_clipboard", return_value=(True, None)
        ) as mock_write:
            dispatched = clipboard_module.copy_selection_to_clipboard_async(app)

        assert dispatched is True
        # The write must NOT have happened on the calling thread — it is
        # deferred to the worker that run_worker schedules.
        mock_write.assert_not_called()
        app.run_worker.assert_called_once()
        kwargs = app.run_worker.call_args.kwargs
        assert kwargs.get("thread") is True
        assert kwargs.get("exit_on_error") is False

        # Running the scheduled work performs the write + notify.
        work = app.run_worker.call_args.args[0]
        with patch.object(
            clipboard_module, "_write_to_clipboard", return_value=(True, None)
        ) as mock_write_in_worker:
            work()
        mock_write_in_worker.assert_called_once_with("payload")
        app.notify.assert_called_once()

    def test_returns_false_and_skips_worker_without_selection(self) -> None:
        app = MagicMock()
        app.query.return_value = []

        dispatched = clipboard_module.copy_selection_to_clipboard_async(app)

        assert dispatched is False
        app.run_worker.assert_not_called()

    def test_falls_back_to_sync_write_when_worker_dispatch_fails(self) -> None:
        _, app = _setup_copy("payload")
        app.run_worker.side_effect = RuntimeError("no event loop")

        with patch.object(
            clipboard_module, "_write_to_clipboard", return_value=(True, None)
        ) as mock_write:
            dispatched = clipboard_module.copy_selection_to_clipboard_async(app)

        assert dispatched is True
        # When the worker cannot be scheduled we still copy, synchronously.
        mock_write.assert_called_once_with("payload")
        app.notify.assert_called_once()
