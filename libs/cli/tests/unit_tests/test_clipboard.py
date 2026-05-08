"""Tests for clipboard helpers."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bog_agents_cli import clipboard as clipboard_module


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
    app.copy_to_clipboard.side_effect = RuntimeError("app clipboard unavailable")

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
        with patch.object(clipboard_module, "_copy_windows_clip"):
            with patch.object(clipboard_module.sys, "platform", "win32"):
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

        with (
            patch.object(clipboard_module, "_copy_windows_clip"),
            patch.object(clipboard_module.sys, "platform", "win32"),
        ):
            clipboard_module.copy_selection_to_clipboard(app)

        msg = app.notify.call_args.args[0]
        assert "2 selections" in msg
        assert "first" not in msg
        assert "second" not in msg
