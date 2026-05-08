"""Context manager that ensures ``sys.stderr`` has a valid OS file descriptor.

Background — ``[Errno 9] Bad file descriptor`` on Windows
=========================================================

When ``mcp.client.stdio.stdio_client`` spawns an MCP server subprocess,
it passes ``sys.stderr`` as the child's ``stderr`` (this is its default
``errlog`` parameter). On Windows, ``subprocess.Popen._get_handles``
calls ``msvcrt.get_osfhandle(stderr.fileno())`` to translate the
Python file's fd into an OS handle. If the parent process's
``sys.stderr`` doesn't have a valid OS-level handle —

- Python 3.13 on Windows occasionally produces this state under TUI/
  terminal-wrapper scenarios where stderr was reopened or duplicated
  through layers that don't propagate the OS handle cleanly.
- ``bog-agents`` started under a wrapper that piped stderr (e.g.
  ``bog-agents 2>err.log`` from certain shells) can also break the
  handle on subsequent reads.
- Any code earlier in the process that called ``os.close(2)`` and
  later restored ``sys.stderr`` to a Python-level wrapper would
  produce the same symptom.

— ``msvcrt.get_osfhandle()`` raises ``OSError: [Errno 9] Bad file
descriptor`` and the entire MCP load chain dies. The user sees the
error in the chat but can't fix it from inside the agent because the
MCP servers (which they were going to use to fix it) failed to
initialize.

The Fix
=======

Detect at spawn-time whether ``sys.stderr.fileno()`` produces a
working OS handle. If it does, do nothing (zero overhead). If it
doesn't, redirect ``sys.stderr`` to a real log file at
``~/.bog-agents/logs/mcp-stderr.log`` for the duration of the
``__aenter__`` call. The subprocess inherits the log file's fd
(guaranteed valid), and the parent's ``sys.stderr`` is restored as
soon as ``__aexit__`` runs.

The MCP server's own stderr stream now lands in the log file —
useful for debugging an MCP server that prints diagnostics. We rotate
the log conservatively (truncate at 5 MB) so it doesn't grow without
bound.

The context manager is **async** so it can be used directly with
``async with`` blocks around ``await client.session(...)``. It is
also safe to use from sync code via the ``contextlib.suppress``-style
``__enter__`` / ``__exit__`` pair.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import IO, Any

logger = logging.getLogger(__name__)

# Cap MCP stderr log at 5 MB. When exceeded we truncate-on-open.
_MAX_LOG_BYTES = 5 * 1024 * 1024


def _ensure_log_path() -> Path:
    """Return ``~/.bog-agents/logs/mcp-stderr.log``, creating the dir."""
    log_dir = Path.home() / ".bog-agents" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "mcp-stderr.log"


def _stderr_handle_is_usable() -> bool:
    """Return True iff a subprocess can inherit ``sys.stderr`` cleanly.

    On Windows this is the strict test: ``msvcrt.get_osfhandle`` must
    succeed on ``sys.stderr.fileno()``. On non-Windows, any successful
    ``fileno()`` is sufficient.

    The broad ``except Exception`` is intentional — pytest capture
    wrappers, a closed stream, a non-file-backed ``sys.stderr``
    replacement, or a Python 3.13 Windows regression each surface
    a different exception type. We treat all of them as "not
    usable" so the safer redirect path runs.
    """
    try:
        fd = sys.stderr.fileno()
    except Exception:  # any failure here means the fd is unusable
        return False

    if not sys.platform.startswith("win"):
        return True

    try:
        import msvcrt  # type: ignore[import-not-found]

        msvcrt.get_osfhandle(fd)
    except Exception:  # any failure means subprocess inheritance will break
        return False
    return True


def _open_safe_stderr() -> IO[str]:
    """Open the MCP-stderr log file with truncation if oversized.

    ``buffering=1`` enables line buffering so each MCP server line lands
    on disk promptly, which matters for live debugging.
    """
    path = _ensure_log_path()
    try:
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            path.unlink()
    except OSError:
        # Truncation is best-effort. If we can't unlink (e.g. another
        # process holds the file on Windows), fall through to append.
        logger.debug("Could not truncate %s; appending instead", path, exc_info=True)
    # ``a`` = append, line buffered. We always write fresh; the previous
    # session's log (if any) stays appended for context.
    return path.open("a", encoding="utf-8", buffering=1, errors="replace")


@contextlib.contextmanager
def safe_subprocess_stderr() -> Generator[None, None, None]:
    """Ensure ``sys.stderr`` has a valid OS fd for the duration of the block.

    Usage::

        with safe_subprocess_stderr():
            subprocess.run(["my-tool"])  # inherits a valid stderr

    No-op when ``sys.stderr`` is already usable. When it isn't, redirects
    to ``~/.bog-agents/logs/mcp-stderr.log`` for the block, then restores
    the original ``sys.stderr``.
    """
    if _stderr_handle_is_usable():
        yield
        return

    original = sys.stderr
    log_file: IO[str] | None = None
    try:
        log_file = _open_safe_stderr()
    except OSError:
        # Last-resort fallback: use os.devnull so the subprocess still
        # gets a valid fd. Loses MCP server stderr output but doesn't
        # crash the spawn, which is the priority.
        logger.warning(
            "Could not open MCP stderr log; falling back to os.devnull",
            exc_info=True,
        )
        try:
            log_file = Path(os.devnull).open("a", encoding="utf-8")  # noqa: SIM115  # closed in finally below
        except OSError:
            # Truly stuck — yield without redirect and let MCP fail with
            # the original message. The caller already catches it.
            logger.exception("Could not open os.devnull either; spawn will fail")
            yield
            return

    sys.stderr = log_file
    try:
        yield
    finally:
        sys.stderr = original
        try:
            log_file.close()
        except OSError:
            logger.debug("Could not close mcp-stderr log", exc_info=True)


@contextlib.asynccontextmanager
async def asafe_subprocess_stderr() -> Any:  # noqa: ANN401, RUF029  # async context manager wrapper around the sync version; runtime-typed yield
    """Async variant of :func:`safe_subprocess_stderr`.

    Mirrors the sync version exactly — no async work happens in the
    redirect itself, but using ``async with`` is more ergonomic when
    wrapping ``await`` calls in code that's already async.

    Yields:
        ``None`` — the context exists purely for its setup/teardown
        side-effect (sys.stderr swap).
    """
    with safe_subprocess_stderr():
        yield


def diagnostic_info() -> dict[str, Any]:
    """Return a snapshot of the stderr state — useful for ``doctor``.

    Caller can drop this into a structured-log line so support requests
    that hit the EBADF symptom carry enough context to triage without a
    follow-up round.
    """
    info: dict[str, Any] = {
        "platform": sys.platform,
        "stderr_class": type(sys.stderr).__name__,
        "stderr_isatty": False,
        "stderr_usable": False,
    }
    try:
        info["stderr_isatty"] = sys.stderr.isatty()
    except (OSError, AttributeError):
        pass
    info["stderr_usable"] = _stderr_handle_is_usable()
    info["log_path"] = str(_ensure_log_path())
    return info


__all__ = [
    "asafe_subprocess_stderr",
    "diagnostic_info",
    "safe_subprocess_stderr",
]
