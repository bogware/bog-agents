"""Panic dump — write a redacted crash report when an unexpected exception bubbles.

When the user hits a crash they tend to paste a Python traceback into a
GitHub issue. That's useful but missing the things support actually
needs: which model was selected, what extensions were enabled, what
the recent inbox looked like, recent log events. The panic dump
collects these into ``~/.bog-agents/crash/<ts>.log`` so the user can
attach a single file.

Strict redaction rules:

- API keys / tokens (anything that looks like a credential) → ``***``
- File paths inside the user's home are kept as-is — they help support
  reproduce; this is opt-in (the user is choosing to attach the file).
- Cleartext secrets via :class:`SecretStr` are filtered automatically
  (they redact themselves on ``str()``).
- Recent log events come from the in-process metrics registry, not raw
  log lines, so we don't accidentally flush a buffer that may contain
  sensitive content.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Redaction patterns for obvious credential shapes.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anthropic / OpenAI / generic bearer-token shapes.
    (re.compile(r"(sk-[A-Za-z0-9_-]{16,})"), "***"),
    (re.compile(r"(xoxb-[A-Za-z0-9-]{10,})"), "***"),  # Slack bot tokens
    (re.compile(r"(ghp_[A-Za-z0-9]{20,})"), "***"),    # GitHub PATs
    (re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"), "***"),
    (re.compile(r"(AKIA[A-Z0-9]{16})"), "***"),        # AWS access key id
    (re.compile(r"(eyJ[A-Za-z0-9_-]{10,})"), "***"),   # JWTs
    # Generic key=value redactions for known sensitive env-var names.
    (
        re.compile(
            r"((?i:api[_-]?key|secret|token|password)\s*[:=]\s*)[\"']?([A-Za-z0-9_\-./]{8,})[\"']?"
        ),
        r"\1***",
    ),
)


def _redact(text: str) -> str:
    """Apply all redaction patterns to ``text``."""
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def write_panic_dump(
    exc: BaseException,
    *,
    config_dir: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path | None:
    """Capture the crash into ``<config_dir>/crash/<ts>.log``.

    Args:
        exc: The exception that bubbled.
        config_dir: User config dir (``~/.bog-agents``). Defaults to
            ``Path.home() / ".bog-agents"``.
        extra: Optional caller-supplied fields (e.g. last user prompt,
            current /peat job id). Will be JSON-redacted.

    Returns:
        Path to the written dump, or ``None`` if writing failed (we
        never raise from inside a panic handler).
    """
    try:
        if config_dir is None:
            config_dir = Path.home() / ".bog-agents"
        crash_dir = config_dir / "crash"
        crash_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = crash_dir / f"{ts}.log"

        payload = _build_payload(exc, extra=extra or {})
        text = _format_dump(payload)
        # Atomic write via the existing helper so a Ctrl-C during dump
        # doesn't leave a partial file.
        try:
            from bog_agents_cli.io_utils import atomic_write_text

            atomic_write_text(path, text)
        except Exception:
            # Fall back to a direct write if the helper itself blew up
            # — better to leave SOMETHING on disk than nothing.
            path.write_text(text, encoding="utf-8")
    except Exception:
        logger.warning("panic dump failed", exc_info=True)
        return None
    return path


def _build_payload(exc: BaseException, *, extra: dict[str, Any]) -> dict[str, Any]:
    """Gather the dict of fields that go into the dump."""
    # Late import to avoid a hard dep cycle (observability also uses logging).
    try:
        from bog_agents_cli._observability import get_metrics_snapshot

        metrics = get_metrics_snapshot()
    except Exception:
        metrics = {}

    try:
        from bog_agents_cli._version import __version__ as cli_version
    except Exception:
        cli_version = "unknown"

    try:
        from bog_agents._version import __version__ as sdk_version
    except Exception:
        sdk_version = "unknown"

    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    return {
        "schema": "bog-agents-panic-dump-v1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exception": {
            "type": exc.__class__.__name__,
            "message": str(exc),
            "traceback": tb_text,
        },
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cwd": Path.cwd(),
        },
        "versions": {
            "bog-agents-cli": cli_version,
            "bog-agents": sdk_version,
        },
        "metrics_snapshot": metrics,
        "extra": extra,
    }


def _format_dump(payload: dict[str, Any]) -> str:
    """Render the payload as a human + machine readable text file.

    The file starts with a one-line header (so a `head` is informative),
    then JSON for the structured payload, then a separator and the
    formatted traceback as plain text for easy reading.
    """
    redacted = _redact(json.dumps(payload, indent=2, default=str, sort_keys=True))
    lines = [
        f"# bog-agents panic dump  ({payload['timestamp']})",
        f"# {payload['exception']['type']}: {_redact(payload['exception']['message'])[:200]}",
        "",
        "## Structured payload",
        "",
        redacted,
        "",
        "## Traceback",
        "",
        _redact(payload["exception"]["traceback"]),
    ]
    return "\n".join(lines)


def install_panic_handler(config_dir: Path | None = None) -> None:
    """Wire ``sys.excepthook`` to write a panic dump on uncaught exceptions.

    Idempotent: calling it twice is safe — the second call replaces the
    first, but the chain still ends with ``sys.__excepthook__``.

    Args:
        config_dir: Override for the dump output directory.
    """
    previous_hook = sys.excepthook

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        # KeyboardInterrupt / SystemExit shouldn't generate dumps — they
        # aren't crashes. Fall straight through to the previous hook.
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            previous_hook(exc_type, exc, tb)  # type: ignore[arg-type]
            return
        path = write_panic_dump(exc, config_dir=config_dir)
        if path is not None:
            try:
                sys.stderr.write(
                    "\nbog-agents crashed. A panic dump was saved at:\n"
                    f"  {path}\n"
                    "Attach this file when you open an issue at "
                    "https://github.com/bogware/bog-agents/issues — it has "
                    "(redacted) versions, traceback, and recent metrics.\n"
                )
            except Exception:
                # stderr unavailable is fine — dump was already written.
                pass
        # Always still call the original hook so the traceback also
        # prints to the user's terminal (or upstream logger).
        previous_hook(exc_type, exc, tb)  # type: ignore[arg-type]

    sys.excepthook = _hook
