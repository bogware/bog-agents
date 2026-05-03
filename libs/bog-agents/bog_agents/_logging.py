"""Structured logging + run/turn correlation for bog-agents.

This module provides three things:

1. ``get_logger(name)`` — preferred logger factory; emits ``run_id`` and
   ``turn_id`` from contextvars on every record, so a single agent run
   can be greppable across SDK middleware, CLI subprocesses, and the
   daemon.
2. Context-vars (``run_id_var``, ``turn_id_var``) plus convenience
   context managers (:func:`bind_run`, :func:`bind_turn`) so call sites
   only have to declare the boundary, not thread the IDs by hand.
3. An opt-in JSON formatter, activated by setting
   ``BOG_AGENTS_LOG_FORMAT=json`` in the environment. The default
   formatter stays human-readable so day-to-day developer output is
   unchanged.

The module is intentionally dependency-free (stdlib only) so importing
``bog_agents`` does not pay any extra cost.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

# Public correlation-id ContextVars. Defaults are empty strings so logs
# from boot code (before any bind_run) still render cleanly.
run_id_var: ContextVar[str] = ContextVar("bog_agents_run_id", default="")
turn_id_var: ContextVar[str] = ContextVar("bog_agents_turn_id", default="")


class _CorrelationFilter(logging.Filter):
    """Inject ``run_id`` / ``turn_id`` from ContextVars onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.turn_id = turn_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    """Minimal structured formatter — one JSON object per line.

    Captures the standard fields (timestamp, level, logger, message)
    plus correlation IDs and any extra dict the caller passed via
    ``extra=``. ``exc_info`` is folded into the ``error`` key when set.
    """

    _STANDARD_KEYS: frozenset[str] = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "asctime",
            "taskName",
            "message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = getattr(record, "run_id", "") or ""
        turn_id = getattr(record, "turn_id", "") or ""
        if run_id:
            payload["run_id"] = run_id
        if turn_id:
            payload["turn_id"] = turn_id
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        # Forward any user-supplied ``extra=`` keys.
        for key, value in record.__dict__.items():
            if key in self._STANDARD_KEYS or key in payload or key in ("run_id", "turn_id"):
                continue
            if key.startswith("_"):
                continue
            safe_value: Any = value
            try:
                json.dumps(safe_value, default=str)
            except (TypeError, ValueError):
                safe_value = str(safe_value)
            payload[key] = safe_value
        return json.dumps(payload, default=str)


_HUMAN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [run=%(run_id)s turn=%(turn_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _select_format() -> str:
    raw = os.environ.get("BOG_AGENTS_LOG_FORMAT", "").strip().lower()
    return "json" if raw == "json" else "human"


def _root_logger() -> logging.Logger:
    return logging.getLogger("bog_agents")


_configured: dict[str, bool] = {"done": False}


def configure(*, force: bool = False) -> None:
    """Install the correlation filter and pick a formatter for the root logger.

    Idempotent: subsequent calls are no-ops unless ``force=True``. We never
    touch the root Python logger — only the ``bog_agents`` namespace — so
    application owners can keep their own logging config intact.
    """
    if _configured["done"] and not force:
        return

    root = _root_logger()
    # Clear any previous bog-agents handler we installed. Don't touch
    # handlers the host application added.
    for h in list(root.handlers):
        if getattr(h, "_bog_agents_handler", False):
            root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler._bog_agents_handler = True  # type: ignore[attr-defined]
    handler.addFilter(_CorrelationFilter())

    if _select_format() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_HUMAN_FORMAT, datefmt=_DATE_FORMAT))

    root.addHandler(handler)
    # Don't propagate to Python root — host apps may have their own
    # handlers that would render twice.
    root.propagate = False

    _configured["done"] = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``bog_agents`` namespace with correlation IDs.

    Preferred over plain ``logging.getLogger(__name__)`` because the
    returned logger's records carry ``run_id`` and ``turn_id`` attributes
    that the JSON formatter (and ``%(run_id)s`` in the human format) can
    pick up automatically.
    """
    configure()
    if name == "bog_agents" or name.startswith("bog_agents."):
        return logging.getLogger(name)
    # Re-namespace third-party-style ``__name__`` strings under bog_agents.
    return logging.getLogger(f"bog_agents.ext.{name}")


@contextlib.contextmanager
def bind_run(run_id: str | None = None) -> Iterator[str]:
    """Bind ``run_id`` for the duration of the ``with`` block.

    Pass an explicit ID to correlate logs with an external tracker (e.g.
    LangSmith run id). Pass ``None`` to auto-generate a hex8 token.
    """
    rid = run_id or uuid.uuid4().hex[:12]
    token = run_id_var.set(rid)
    try:
        yield rid
    finally:
        run_id_var.reset(token)


@contextlib.contextmanager
def bind_turn(turn_id: str | None = None) -> Iterator[str]:
    """Bind ``turn_id`` for one agent turn (one wrap_model_call invocation)."""
    tid = turn_id or uuid.uuid4().hex[:8]
    token = turn_id_var.set(tid)
    try:
        yield tid
    finally:
        turn_id_var.reset(token)


__all__ = [
    "bind_run",
    "bind_turn",
    "configure",
    "get_logger",
    "run_id_var",
    "turn_id_var",
]
