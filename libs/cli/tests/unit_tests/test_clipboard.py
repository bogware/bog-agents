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
