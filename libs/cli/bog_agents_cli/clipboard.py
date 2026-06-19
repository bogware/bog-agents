"""Clipboard utilities for bog-agents-cli."""

from __future__ import annotations

import base64
import logging
import os
import pathlib
import shutil
import subprocess  # noqa: S404  # native clipboard helpers require subprocess
import sys
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App

_PREVIEW_MAX_LENGTH = 40


def _subprocess_creationflags() -> int:
    """Return platform-appropriate subprocess flags for clipboard helpers."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _copy_osc52(text: str) -> None:
    """Copy text using OSC 52 escape sequence (works over SSH/tmux)."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    osc52_seq = f"\033]52;c;{encoded}\a"
    if os.environ.get("TMUX"):
        osc52_seq = f"\033Ptmux;\033{osc52_seq}\033\\"

    with pathlib.Path("/dev/tty").open("w", encoding="utf-8") as tty:
        tty.write(osc52_seq)
        tty.flush()


def _copy_windows_clip(text: str) -> None:
    """Copy text to the Windows clipboard using `clip.exe`."""
    subprocess.run(
        ["clip.exe"],
        input=text,
        text=True,
        check=True,
        creationflags=_subprocess_creationflags(),
    )


def _read_windows_clipboard() -> str:
    """Read text from the Windows clipboard using PowerShell."""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-Clipboard -Raw",
        ],
        check=True,
        capture_output=True,
        text=True,
        creationflags=_subprocess_creationflags(),
    )
    return result.stdout


def _read_command_output(command: list[str]) -> str:
    """Read clipboard text from a command-line helper."""
    result = subprocess.run(  # noqa: S603  # commands are fixed clipboard helper invocations
        command,
        check=True,
        capture_output=True,
        text=True,
        creationflags=_subprocess_creationflags(),
    )
    return result.stdout


def read_clipboard_text() -> str | None:
    """Read text from the system clipboard when possible."""
    try:
        import pyperclip

        text = pyperclip.paste()
        return text if isinstance(text, str) and text else None
    except (ImportError, RuntimeError, TypeError):
        pass

    try:
        if sys.platform == "win32":
            text = _read_windows_clipboard()
            return text or None
        if sys.platform == "darwin" and shutil.which("pbpaste"):
            text = _read_command_output(["pbpaste"])
            return text or None
        if shutil.which("wl-paste"):
            text = _read_command_output(["wl-paste", "-n"])
            return text or None
        if shutil.which("xclip"):
            text = _read_command_output(["xclip", "-selection", "clipboard", "-o"])
            return text or None
        if shutil.which("xsel"):
            text = _read_command_output(["xsel", "--clipboard", "--output"])
            return text or None
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        logger.debug("Clipboard read failed: %s", e, exc_info=True)

    return None


def _gather_selection(app: App) -> tuple[str, str] | None:
    """Collect selected text from app widgets and compose a notification.

    Walks the widget tree for every widget's ``text_selection`` and joins the
    selected fragments. This MUST run on the UI thread — the Textual widget
    tree is not safe to traverse from a worker thread.

    Returns a ``(combined_text, notify_message)`` pair, or ``None`` when
    nothing is selected.
    """
    selected_texts: list[str] = []
    seen: set[str] = set()

    for widget in app.query("*"):
        if not hasattr(widget, "text_selection") or not widget.text_selection:
            continue

        selection = widget.text_selection

        if selection.end is None:
            continue

        try:
            result = widget.get_selection(selection)
        except (AttributeError, TypeError, ValueError, IndexError) as e:
            logger.debug(
                "Failed to get selection from widget %s: %s",
                type(widget).__name__,
                e,
                exc_info=True,
            )
            continue

        if not result:
            continue

        selected_text, _ = result
        # Normalise + dedupe. Nested widgets often report the same
        # selection on both the leaf and its container; without this we
        # ended up with duplicate copies in the combined text and a
        # confusing preview that mixed whitespace with content.
        normalised = selected_text.strip()
        if normalised and normalised not in seen:
            seen.add(normalised)
            selected_texts.append(selected_text)

    if not selected_texts:
        return None

    combined_text = "\n".join(selected_texts)

    # Compose a *short* notification. The previous version echoed the
    # first 40 chars of the actual content (``"some text..." copied``)
    # which collided visually with the click-to-show-timestamp toast
    # (``May 5, 2:45 PM``) — users reported seeing both at once and
    # finding it noisy. Now we just say "Copied N chars" (or
    # "[Copied text truncated] copied!" for the multi-selection case)
    # and let the timestamp toast carry the date when relevant.
    char_count = len(combined_text)
    if len(selected_texts) > 1:
        notify_message = f"Copied {len(selected_texts)} selections ({char_count} chars)"
    elif char_count > _PREVIEW_MAX_LENGTH:
        notify_message = f"[Copied text truncated] copied! ({char_count} chars)"
    else:
        notify_message = f"Copied! ({char_count} chars)"

    return combined_text, notify_message


def _clipboard_write_methods() -> list[Callable[[str], None]]:
    """Return clipboard-write backends, most-reliable/fastest first.

    `pyperclip` is preferred on every platform: on Windows it uses the native
    Win32 API (ctypes, no subprocess) and writes ``CF_UNICODETEXT``, on macOS
    it shells to ``pbcopy`` and on Linux to ``xclip``/``xsel``. The platform
    helpers are *fallbacks* only:

    * ``clip.exe`` for the rare Windows box where pyperclip's backend fails;
    * OSC 52 (written to ``/dev/tty``) for headless / SSH POSIX sessions with
      no native clipboard tool installed.

    Earlier versions put ``clip.exe`` *first* on Windows. That made every copy
    spawn a subprocess (hundreds of ms) on the UI thread — the source of the
    select-to-copy lag — and routed text through the console code page, which
    mangled non-ASCII. pyperclip is both faster and Unicode-correct, so it now
    leads.
    """
    methods: list[Callable[[str], None]] = []

    try:
        import pyperclip

        methods.append(pyperclip.copy)
    except ImportError:
        pass

    if sys.platform == "win32":
        methods.append(_copy_windows_clip)
    else:
        # OSC 52 covers remote/SSH POSIX sessions where pyperclip's backend
        # (xclip/xsel/pbcopy) may be missing.
        methods.append(_copy_osc52)

    return methods


def _write_to_clipboard(text: str) -> tuple[bool, Exception | None]:
    """Write ``text`` to the system clipboard, trying each backend in turn.

    Pure I/O with no Textual app dependency, so it is safe to call from a
    worker thread. Returns ``(succeeded, last_error)``.
    """
    last_error: Exception | None = None
    for copy_fn in _clipboard_write_methods():
        try:
            copy_fn(text)
        except (OSError, RuntimeError, TypeError, subprocess.SubprocessError) as e:
            last_error = e
            logger.debug(
                "Clipboard copy method %s failed: %s",
                getattr(copy_fn, "__name__", repr(copy_fn)),
                e,
                exc_info=True,
            )
            continue
        return True, last_error

    return False, last_error


def _notify_copy_result(
    app: App,
    *,
    success: bool,
    last_error: Exception | None,
    notify_message: str,
) -> None:
    """Emit exactly one toast describing the copy outcome.

    `App.notify` is documented thread-safe, so this is safe to call from the
    background worker that performed the write.
    """
    if success:
        try:
            app.notify(notify_message, severity="information", timeout=2, markup=False)
        except Exception:
            # The clipboard write already succeeded — don't surface a failure
            # just because the notify call itself blew up for some reason.
            logger.debug("clipboard notify failed", exc_info=True)
        return

    app.notify(
        "Failed to copy - no clipboard method available"
        + (f" (last error: {last_error.__class__.__name__})" if last_error else ""),
        severity="warning",
        timeout=3,
    )


def copy_selection_to_clipboard(app: App) -> bool:
    """Copy the current selection to the clipboard synchronously.

    Gathers the selection, writes it, and emits exactly one notification. This
    blocks until the clipboard write finishes, so the interactive TUI should
    use `copy_selection_to_clipboard_async` instead (it keeps the event loop
    free). Retained for headless callers and tests.
    """
    gathered = _gather_selection(app)
    if gathered is None:
        return False

    combined_text, notify_message = gathered
    success, last_error = _write_to_clipboard(combined_text)
    _notify_copy_result(
        app,
        success=success,
        last_error=last_error,
        notify_message=notify_message,
    )
    return success


def copy_selection_to_clipboard_async(app: App) -> bool:
    """Copy the selection without blocking the UI event loop.

    The selection is gathered on the calling (UI) thread — the widget tree is
    not safe to walk from another thread — then the clipboard write and the
    result toast run on a background thread worker.

    This is what fixes the select-to-copy regression where the whole UI froze
    for a beat on mouse-release and the "Copied" toast didn't appear until the
    user alt-tabbed away and back: the freeze was the ``clip.exe`` subprocess
    spawning on the asyncio event loop, and the toast couldn't paint until a
    later refresh (which the focus change forced).

    Returns True if a selection was found and a write dispatched, False if
    there was nothing selected.
    """
    gathered = _gather_selection(app)
    if gathered is None:
        return False

    combined_text, notify_message = gathered

    def _write_and_notify() -> None:
        success, last_error = _write_to_clipboard(combined_text)
        _notify_copy_result(
            app,
            success=success,
            last_error=last_error,
            notify_message=notify_message,
        )

    try:
        app.run_worker(
            _write_and_notify,
            thread=True,
            exclusive=False,
            exit_on_error=False,
            group="clipboard",
            name="copy-selection",
        )
    except Exception:
        # If the worker can't be scheduled, fall back to a synchronous write so
        # the copy still happens (at the cost of one brief blocking call). A
        # copy attempt must never crash the app.
        logger.debug(
            "clipboard worker dispatch failed; copying synchronously",
            exc_info=True,
        )
        success, last_error = _write_to_clipboard(combined_text)
        _notify_copy_result(
            app,
            success=success,
            last_error=last_error,
            notify_message=notify_message,
        )

    return True
