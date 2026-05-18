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
        import inspect

        import mcp.client.stdio as _mcp_stdio
    except ImportError:
        return patches

    # Resolve the target functions, then walk the ``__wrapped__`` chain
    # so decorated functions (asynccontextmanager) get patched at the
    # layer where defaults actually live.
    #
    # The signatures we care about (paraphrased): both ``stdio_client``
    # and ``_create_platform_compatible_process`` take an ``errlog``
    # parameter defaulting to ``sys.stderr``. The exact param positions
    # differ — that's why we use ``inspect.signature`` below.
    #
    # Note: ``stdio_client`` always passes ``errlog=`` explicitly to
    # ``_create_platform_compatible_process``, so patching the latter's
    # default is technically belt-and-suspenders. We do it anyway in
    # case any future caller relies on the default.
    #
    # CRITICAL: we use parameter NAMES, not positional index. A previous
    # version patched ``defaults[-1]`` which for
    # ``_create_platform_compatible_process`` is the ``cwd`` slot
    # (``None``) — silently corrupting ``cwd`` to a file pointer while
    # leaving the actual ``errlog`` (broken stderr) unchanged.
    for name in ("stdio_client", "_create_platform_compatible_process"):
        fn = getattr(_mcp_stdio, name, None)
        if fn is None:
            continue
        target = fn
        while hasattr(target, "__wrapped__"):
            inner = target.__wrapped__
            if hasattr(inner, "__defaults__"):
                target = inner
            else:
                break

        defaults = getattr(target, "__defaults__", None)
        if not defaults:
            continue

        # Build (param_name → defaults_index) using ``inspect`` so the
        # patch tracks the actual ``errlog`` position regardless of
        # signature changes between MCP versions.
        try:
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            logger.debug("Could not inspect signature of %r", target)
            continue
        params_with_defaults = [
            p
            for p in sig.parameters.values()
            if p.default is not inspect.Parameter.empty
        ]
        if len(params_with_defaults) != len(defaults):
            # Signature mismatch (e.g. **kwargs in chain). Skip rather
            # than guess.
            logger.debug(
                "Param/default count mismatch on %r: %d params, %d defaults",
                target,
                len(params_with_defaults),
                len(defaults),
            )
            continue
        try:
            errlog_idx = next(
                i for i, p in enumerate(params_with_defaults) if p.name == "errlog"
            )
        except StopIteration:
            # Function doesn't have an ``errlog`` parameter — nothing
            # to patch on this target.
            continue

        # Replace just the errlog slot, leaving every other default
        # untouched.
        new_defaults = tuple(
            safe_file if i == errlog_idx else d for i, d in enumerate(defaults)
        )
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


# Module-level slot for the permanently-installed safe stderr file.
# Held forever (process lifetime) so MCP's per-call subprocess spawns
# always inherit a valid fd. ``None`` = installer was a no-op (stderr
# already usable, or already installed).
_INSTALLED_SAFE_STDERR: IO[str] | None = None
_INSTALL_DONE: bool = False


def install_safe_subprocess_stderr_default() -> bool:
    """Permanently override MCP's default ``errlog`` for the process.

    Idempotent. Returns ``True`` when an override was applied (or was
    already in place from a prior call), ``False`` when the current
    ``sys.stderr`` is already usable for subprocess inheritance and no
    override was needed.

    Why this exists alongside :func:`safe_subprocess_stderr`:
    ----------------------------------------------------------

    The context-manager version restores the original ``errlog``
    defaults on exit. That works when the MCP subprocess is spawned
    *inside* the with-block (the original load-time pattern with
    persistent sessions). It does NOT work for **per-call sessions** —
    the pattern we now use, where the subprocess is spawned each time
    the agent invokes an MCP tool. Those spawns happen deep inside
    LangGraph's tool execution, far from any context manager we
    control. Without a permanent install, every per-call spawn would
    use the broken default.

    The fix is unbalanced by design: we open a log file once, hold it
    for the process lifetime, and patch the MCP defaults to point at
    it. The original ``sys.stderr`` was the broken one — there is
    nothing to "restore" to.
    """
    global _INSTALLED_SAFE_STDERR, _INSTALL_DONE  # noqa: PLW0603

    if _INSTALL_DONE:
        return _INSTALLED_SAFE_STDERR is not None

    if _stderr_handle_is_usable():
        # Nothing to do — subprocess inheritance will work as-is.
        _INSTALL_DONE = True
        return False

    try:
        log_file = _open_safe_stderr()
    except OSError:
        logger.warning(
            "Could not open MCP stderr log for permanent install; "
            "MCP spawns may fail with EBADF on Windows",
            exc_info=True,
        )
        _INSTALL_DONE = True
        return False

    # Patch MCP defaults *permanently* — we deliberately discard the
    # restore tuple. ``log_file`` is held in the module slot so it is
    # not GC'd for the process lifetime.
    _patch_mcp_stdio_default_errlog(log_file)
    _INSTALLED_SAFE_STDERR = log_file
    _INSTALL_DONE = True
    logger.info(
        "Installed permanent safe stderr override for MCP subprocess spawns (log: %s)",
        _ensure_log_path(),
    )
    return True


@contextlib.asynccontextmanager
async def asafe_subprocess_stderr() -> Any:  # noqa: ANN401  # async context manager wrapper around the sync version; runtime-typed yield
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
    "install_safe_subprocess_stderr_default",
    "safe_subprocess_stderr",
    "tail_mcp_stderr_log",
]
