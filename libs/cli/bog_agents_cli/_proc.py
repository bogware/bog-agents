"""Cross-platform process inspection helpers.

Centralises the Windows-vs-POSIX awkwardness around `os.kill(pid, 0)`.

On POSIX, sending signal 0 to a non-existent PID raises
`ProcessLookupError` and the standard try/except idiom works.

On Windows, `os.kill(pid, 0)` for a dead PID can raise a generic
`OSError [WinError 87] "The parameter is incorrect"` *and* CPython
sometimes propagates this as a `SystemError: returned a result with
an exception set` — a known C-level quirk we hit during pass-2
validation. The robust answer on Windows is to ask `tasklist` directly.

`signal.SIGKILL` doesn't exist on Windows either, so the kill helper
falls back to TerminateProcess via `os.kill(pid, signal.SIGTERM)` (which
on Windows is mapped to TerminateProcess in CPython) and skips the
SIGKILL escalation gracefully.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess  # noqa: S404
import sys
from typing import Final

logger = logging.getLogger(__name__)

_TASKLIST_TIMEOUT_SECS: Final[int] = 3


def is_running(pid: int) -> bool:
    """Return True when *pid* names a live process.

    Args:
        pid: Process ID to probe.

    Returns:
        True if the process exists, False otherwise. Catches
        `ProcessLookupError`, `PermissionError`, generic `OSError`
        (Windows WinError 87 family), and `SystemError` (CPython
        Windows quirk) — never raises.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            result = subprocess.run(  # noqa: S603
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=_TASKLIST_TIMEOUT_SECS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return f'"{pid}"' in (result.stdout or "")

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def terminate(pid: int, *, force: bool = False) -> bool:
    """Best-effort termination of *pid*.

    Args:
        pid: Process ID to terminate.
        force: When True, escalate to SIGKILL on POSIX after SIGTERM.
            Ignored on Windows (no SIGKILL — `os.kill(pid, SIGTERM)`
            already maps to TerminateProcess and is hard-stop).

    Returns:
        True if the kill call ran without raising; the caller should
        re-check `is_running(pid)` to confirm exit. False when the
        kill itself failed (PID gone, permission denied, etc.).
    """
    if not is_running(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("SIGTERM to pid %d failed: %s", pid, exc)
        return False

    if force and hasattr(signal, "SIGKILL"):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("SIGKILL to pid %d failed: %s", pid, exc)
    return True
