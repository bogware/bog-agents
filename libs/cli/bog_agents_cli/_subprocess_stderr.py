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


def _patch_mcp_stdio_default_errlog(
    safe_file: IO[str],
) -> list[tuple[Any, Any]]:
    """Override ``mcp.client.stdio``'s captured-at-import ``errlog`` defaults.

    The MCP library defines its stdio entry points as::

        @asynccontextmanager
        async def stdio_client(server, errlog: TextIO = sys.stderr): ...

        async def _create_platform_compatible_process(
            ..., errlog: TextIO = sys.stderr, ...
        ): ...

    Python evaluates ``= sys.stderr`` **once at function definition
    time** (i.e. when ``mcp.client.stdio`` is first imported). When the
    import happens *inside* a running Textual app, ``sys.stderr`` is
    already the ``_PrintCapture`` wrapper that has no usable OS fd on
    Windows. The default ``errlog`` is then frozen to that broken
    wrapper for the rest of the process — ``langchain_mcp_adapters``
    calls ``stdio_client(server_params)`` without ``errlog``, the
    broken default is used, and the spawn dies with EBADF.

    There are TWO subtleties:

    1. ``@asynccontextmanager`` wraps the async generator. The
       resulting ``stdio_client`` object's ``__defaults__`` is ``None``
       (the wrapper takes ``*args, **kwds``). The real defaults live on
       ``stdio_client.__wrapped__.__defaults__``. Patching only the
       outer object is a silent no-op — that's what the previous
       attempt did. We patch the inner function via ``__wrapped__``.

    2. ``_create_platform_compatible_process`` has its OWN
       ``errlog: TextIO = sys.stderr`` default. ``stdio_client``
       passes ``errlog=`` explicitly to it, but if any future caller
       relies on its default we still want it correct. Patch both.

    Returns a list of ``(target_function, original_defaults)`` pairs
    so the caller restores them all on exit. Empty list when MCP isn't
    importable or signatures don't match the expected shape — caller
    proceeds unmodified.
    """
    patches: list[tuple[Any, Any]] = []
    try:
        import mcp.client.stdio as _mcp_stdio
    except ImportError:
        return patches

    # Names whose default is exactly ``(sys.stderr,)`` — both the
    # context-managed entry point and the platform helper. We resolve
    # to ``__wrapped__`` first so the asynccontextmanager case lands
    # on the real generator function rather than the no-op wrapper.
    candidates: list[Any] = []
    for name in ("stdio_client", "_create_platform_compatible_process"):
        fn = getattr(_mcp_stdio, name, None)
        if fn is None:
            continue
        # Walk ``__wrapped__`` chain so decorated functions get patched
        # at the layer where defaults actually live.
        target = fn
        while hasattr(target, "__wrapped__"):
            inner = target.__wrapped__
            if hasattr(inner, "__defaults__"):
                target = inner
            else:
                break
        candidates.append(target)

    for target in candidates:
        defaults = getattr(target, "__defaults__", None)
        if not defaults:
            continue
        # The errlog default is the LAST one in the tuple for both
        # functions — we replace just it, leaving any future-added
        # earlier defaults untouched.
        new_defaults = (*defaults[:-1], safe_file)
        try:
            patches.append((target, defaults))
            target.__defaults__ = new_defaults
        except (AttributeError, TypeError):
            # Some objects (e.g. C-implemented) reject __defaults__
            # mutation. Skip rather than fail the whole spawn.
            logger.debug("Could not patch __defaults__ on %r", target, exc_info=True)
            patches.pop()
    return patches


@contextlib.contextmanager
def safe_subprocess_stderr() -> Generator[None, None, None]:
    """Ensure ``sys.stderr`` has a valid OS fd for the duration of the block.

    Usage::

        with safe_subprocess_stderr():
            subprocess.run(["my-tool"])  # inherits a valid stderr

    No-op when ``sys.stderr`` is already usable. When it isn't, redirects
    to ``~/.bog-agents/logs/mcp-stderr.log`` for the block AND patches
    ``mcp.client.stdio.stdio_client``'s default ``errlog`` to point at
    the same file (see :func:`_patch_mcp_stdio_default_errlog` for why
    that's necessary). Both the global swap and the MCP-default patch
    are restored when the block exits.
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
    # Override MCP's default errlog so calls that omit ``errlog=`` use
    # our safe file rather than the captured-at-import broken wrapper.
    mcp_patches = _patch_mcp_stdio_default_errlog(log_file)
    try:
        yield
    finally:
        sys.stderr = original
        for target, original_defaults in mcp_patches:
            try:
                target.__defaults__ = original_defaults
            except Exception:  # restoration must not raise
                logger.warning(
                    "Could not restore __defaults__ on %r", target, exc_info=True
                )
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


def tail_mcp_stderr_log(max_bytes: int = 2000) -> str:
    """Return the last ``max_bytes`` of the MCP stderr log, or empty string.

    Best-effort: returns ``""`` if the log doesn't exist, can't be
    read, or is empty. Caller can splice the result into an error
    message inline so users see the MCP child's actual stderr without
    chasing a separate log file.

    Synchronous; callers in async contexts should wrap with
    ``asyncio.to_thread`` to avoid blocking the event loop on a slow
    disk.
    """
    path = _ensure_log_path()
    try:
        if not path.exists() or path.stat().st_size == 0:
            return ""
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(max(0, path.stat().st_size - max_bytes))
            return fh.read().strip()
    except OSError:
        logger.debug("Could not tail mcp-stderr log", exc_info=True)
        return ""


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
    "tail_mcp_stderr_log",
]
