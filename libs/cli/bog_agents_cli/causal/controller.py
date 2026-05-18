"""``/causal`` slash-command controller (M3).

The TUI calls :func:`dispatch` with the raw slash-command input; the
controller resolves a per-cwd singleton, opens or resumes a ledger,
and routes the rest to the right renderer.

This is intentionally a thin facade — the data layer is in
``ledger.py``, the recording is in ``middleware.py``, the rendering is
in ``render.py``. The controller just stitches them together for the
TUI handler.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from bog_agents_cli.causal.ledger import (
    CausalLedger,
    list_sessions,
    load_session,
    open_session,
)
from bog_agents_cli.causal.middleware import CausalMiddleware
from bog_agents_cli.causal.render import (
    render_ancestry,
    render_graph,
    render_recent,
    render_session_list,
    render_status,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton registry — one controller per cwd
# ---------------------------------------------------------------------------

_CONTROLLERS: dict[Path, CausalController] = {}
_CONTROLLERS_LOCK = threading.Lock()


def get_controller(working_dir: Path | str) -> CausalController:
    """Return the per-cwd singleton :class:`CausalController`."""
    key = Path(working_dir).resolve()
    with _CONTROLLERS_LOCK:
        if key not in _CONTROLLERS:
            _CONTROLLERS[key] = CausalController(working_dir=key)
        return _CONTROLLERS[key]


def reset_controllers() -> None:
    """Drop every cached controller. Test-only helper."""
    with _CONTROLLERS_LOCK:
        for c in _CONTROLLERS.values():
            c._teardown()
        _CONTROLLERS.clear()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class CausalController:
    """Slash-command facade around the causal ledger + middleware.

    Holds at most one *active* ledger (the one being written to right
    now) and lazily resolves read-only ledgers for other sessions when
    the user asks (``/causal sessions``, ``/causal why`` against an
    old session id).
    """

    def __init__(self, *, working_dir: Path) -> None:
        self._working_dir = working_dir
        self._active: CausalLedger | None = None
        self._middleware: CausalMiddleware | None = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Lifecycle / wiring
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active(self) -> CausalLedger | None:
        return self._active

    @property
    def middleware(self) -> CausalMiddleware | None:
        return self._middleware

    def ensure_active(self, *, actor_label: str = "") -> CausalLedger:
        """Open a fresh ledger + middleware if none exists yet.

        Idempotent. The CLI calls this on first ``/causal on`` and
        again on agent rebuilds.
        """
        if self._active is not None:
            return self._active
        ledger = open_session(self._working_dir)
        self._active = ledger
        self._middleware = CausalMiddleware(
            ledger=ledger,
            enabled=self._enabled,
            actor_label=actor_label,
        )
        return ledger

    def set_enabled(self, on: bool) -> str:
        """``/causal on|off`` — start or stop recording."""
        if on:
            self.ensure_active()
            if self._middleware is not None:
                self._middleware.set_enabled(True)
            self._enabled = True
            return (
                f"Causal recording: ON (session {self._active.session_id}). "  # type: ignore[union-attr]
                "New prompts will be traced."
            )
        if self._middleware is not None:
            self._middleware.set_enabled(False)
        self._enabled = False
        return "Causal recording: OFF (existing ledger preserved on disk)."

    def _teardown(self) -> None:
        if self._active is not None:
            self._active.close()
        self._active = None
        self._middleware = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, args: str) -> str:
        """Top-level ``/trace-mind …`` dispatcher."""
        rest = args.strip()
        if not rest:
            return self._status_or_setup_hint()
        head, _, tail = rest.partition(" ")
        head = head.lower()
        tail = tail.strip()
        if head in ("on", "start"):
            return self.set_enabled(True)
        if head in ("off", "stop"):
            return self.set_enabled(False)
        if head == "status":
            return self._status_or_setup_hint()
        if head in ("last", "recent", "tail"):
            return self._recent(tail)
        if head == "why":
            return self._why(tail)
        if head in ("graph", "tree"):
            return self._graph(tail)
        if head in ("sessions", "list"):
            return self._sessions(tail)
        if head == "replay":
            return self._replay(tail)
        return (
            f"Unknown /trace-mind subcommand: '{head}'.\n\n"
            "Try one of:\n"
            "  /trace-mind                          — show status\n"
            "  /trace-mind on|off                   — toggle recording\n"
            "  /trace-mind last [N]                 — last N events (default 20)\n"
            "  /trace-mind why <event_id>           — causal ancestry tree\n"
            "  /trace-mind graph [N]                — session as a tree (last N)\n"
            "  /trace-mind sessions                 — list recorded sessions\n"
            "  /trace-mind replay <id> [flags]      — time-travel rule replay"
        )

    def _replay(self, tail: str) -> str:
        """Q3: time-travel rule replay — delegates to time_travel.dispatch.

        The outer ``/causal …`` surface owns parsing the ``replay``
        head; from there we hand off so the heavy lifting stays
        cohesive inside :mod:`bog_agents_cli.time_travel`.
        """
        from bog_agents_cli.expert_controller import (
            get_controller as _get_expert,
        )
        from bog_agents_cli.time_travel import dispatch as _replay_dispatch

        def _rules_provider() -> list:
            return list(_get_expert(self._working_dir).middleware.engine.rules)

        active_id = self._active.session_id if self._active is not None else None
        return _replay_dispatch(
            f"/trace-mind replay {tail}",
            working_dir=self._working_dir,
            session_id=active_id,
            rules_provider=_rules_provider,
        )

    # ------------------------------------------------------------------
    # Per-subcommand renderers
    # ------------------------------------------------------------------

    def _status_or_setup_hint(self) -> str:
        if self._active is None:
            return (
                "Causal recording: not yet initialized.\n"
                "Start with /trace-mind on — the next prompt will be traced "
                "and saved under .bog-agents/causal/."
            )
        return f"Recording: {'ON' if self._enabled else 'OFF'}\n" + render_status(
            self._active
        )

    def _recent(self, tail: str) -> str:
        if self._active is None:
            return "No active session — run /trace-mind on first."
        limit = _parse_int(tail, default=20)
        return render_recent(self._active, limit=limit)

    def _why(self, tail: str) -> str:
        if not tail:
            return "Usage: /trace-mind why <event_id>"
        try:
            event_id = int(tail.split(maxsplit=1)[0])
        except (IndexError, ValueError):
            return f"Invalid event id: {tail!r} — expected an integer."
        if self._active is None:
            return "No active session — run /trace-mind on first."
        return render_ancestry(self._active, event_id)

    def _graph(self, tail: str) -> str:
        if self._active is None:
            return "No active session — run /trace-mind on first."
        limit = _parse_int(tail, default=60)
        return render_graph(self._active, limit=limit)

    def _sessions(self, tail: str) -> str:
        sessions = list_sessions(self._working_dir)
        # ``/causal sessions <id>`` → render an old session's graph.
        if tail:
            sid = tail.split(maxsplit=1)[0]
            if sid not in sessions:
                return f"Unknown session id: {sid}"
            events = load_session(self._working_dir, sid)
            # Build a read-only ledger view for the renderer.
            ro = CausalLedger(
                working_dir=self._working_dir,
                session_id=sid,
                existing_events=events,
            )
            ro.close()  # belt-and-suspenders: read-only
            return render_graph(ro, limit=60)
        return render_session_list(sessions, limit=10)


def _parse_int(text: str, *, default: int) -> int:
    if not text:
        return default
    try:
        return max(1, int(text.split(maxsplit=1)[0]))
    except (IndexError, ValueError):
        return default


def dispatch(command_text: str, working_dir: Path | str) -> str:
    """TUI entry point — ``/trace-mind …``.

    Wave V renamed the slash from ``/causal`` to ``/trace-mind``.
    The legacy ``/causal`` prefix is still recognised so users with
    muscle memory don't hit a wall, but the canonical name is
    ``/trace-mind`` and the slash registry only ships that one.
    """
    text = command_text.strip()
    for prefix in ("/trace-mind", "/causal"):
        if text.startswith(prefix):
            return get_controller(working_dir).handle(text[len(prefix) :].strip())
    return f"Unknown trace-mind command: {text}"


__all__ = [
    "CausalController",
    "dispatch",
    "get_controller",
    "reset_controllers",
]
