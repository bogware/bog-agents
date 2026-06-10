"""Stand-alone controller for the ``/sweep`` command (Street Sweeper mode).

Backs the ``/sweep on|off|status|log|aggressive|reset`` slash commands. The
controller owns one long-lived :class:`StreetSweeperMiddleware` instance per
working directory, cached in :data:`_CONTROLLERS`. ``create_cli_agent`` attaches
that same instance to every agent it builds (pointing it at the live backend and
model), so toggling ``/sweep on`` takes effect on the next model call without
rebuilding the graph, and ``/sweep status`` reads the live cumulative log.

The sweeper is **disabled by default** — it is a transparent pass-through until
the user runs ``/sweep on``.

## Metrics — the "bank account"

Two scopes of savings are tracked:

- **Session**: the live :class:`SweepLog` on the middleware (tokens removed,
    estimated dollars, % reduction, per-technique breakdown).
- **Lifetime**: a persistent ledger at ``~/.bog-agents/street_sweeper_ledger.json``
    that accumulates every per-call delta across all sessions, so the running
    total of tokens and dollars the sweeper has saved this user only grows over
    time. The middleware fires an ``on_commit`` hook per swept call; the
    controller folds that delta into the ledger and writes it atomically.

Dollar figures are estimates priced at the model's input-token rate; they are a
pre-cache-discount upper bound (cached tokens bill at a fraction of that rate).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from bog_agents.middleware.street_sweeper import StreetSweeperMiddleware, SweepLog

from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)

_CONTROLLERS: dict[Path, SweepController] = {}
_CONTROLLERS_LOCK = threading.Lock()


def _ledger_path() -> Path:
    """Return the path to the persistent lifetime-savings ledger."""
    return Path.home() / ".bog-agents" / "street_sweeper_ledger.json"


def get_sweep_controller(working_dir: Path | str) -> SweepController:
    """Return the (per-cwd) singleton sweep controller.

    Args:
        working_dir: Project root. Different roots get independent controllers,
            so hopping between repos via ``/cd`` keeps separate session logs.

    Returns:
        The cached controller for `working_dir`.
    """
    key = Path(working_dir).resolve()
    with _CONTROLLERS_LOCK:
        if key not in _CONTROLLERS:
            _CONTROLLERS[key] = SweepController()
        return _CONTROLLERS[key]


def reset_sweep_controllers() -> None:
    """Drop every cached controller. Test-only helper."""
    with _CONTROLLERS_LOCK:
        _CONTROLLERS.clear()


class SweepController:
    """Slash-command façade around a single :class:`StreetSweeperMiddleware`.

    The owned middleware starts disabled; ``create_cli_agent`` attaches it to the
    agent graph and the controller flips its `enabled` flag live. The controller
    also maintains the persistent lifetime ledger via the middleware's
    ``on_commit`` hook.

    Args:
        ledger_path: Override for the lifetime-ledger file (tests). Defaults to
            ``~/.bog-agents/street_sweeper_ledger.json``.
    """

    _LIFETIME_KEYS = ("tokens_saved", "dollars_saved", "actions", "calls")

    def __init__(self, *, ledger_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._ledger_path = ledger_path or _ledger_path()
        self._lifetime = self._load_ledger()
        self._middleware = StreetSweeperMiddleware(
            enabled=False, on_commit=self._record_delta
        )

    @property
    def middleware(self) -> StreetSweeperMiddleware:
        """The underlying middleware (attached to the agent by `create_cli_agent`)."""
        return self._middleware

    # ------------------------------------------------------------------ ledger

    def _load_ledger(self) -> dict[str, float]:
        """Load the lifetime ledger from disk, or start a fresh one."""
        base = dict.fromkeys(self._LIFETIME_KEYS, 0.0)
        try:
            if self._ledger_path.exists():
                data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
                for key in self._LIFETIME_KEYS:
                    if isinstance(data.get(key), (int, float)):
                        base[key] = data[key]
        except Exception:
            logger.debug(
                "street sweeper: could not read ledger at %s",
                self._ledger_path,
                exc_info=True,
            )
        return base

    def _record_delta(self, delta: dict[str, Any]) -> None:
        """Fold one per-call savings delta into the lifetime ledger and persist it.

        Registered as the middleware's ``on_commit`` hook. Thread-safe — the
        sweeper can fire from background worker threads sharing this singleton.

        Args:
            delta: Per-call delta with `tokens_saved`, `dollars_saved`, `actions`.
        """
        with self._lock:
            self._lifetime["tokens_saved"] += float(delta.get("tokens_saved", 0))
            self._lifetime["dollars_saved"] += float(delta.get("dollars_saved", 0.0))
            self._lifetime["actions"] += float(delta.get("actions", 0))
            self._lifetime["calls"] += 1.0
            try:
                atomic_write_text(
                    self._ledger_path, json.dumps(self._lifetime), encoding="utf-8"
                )
            except Exception:
                logger.debug(
                    "street sweeper: could not persist ledger to %s",
                    self._ledger_path,
                    exc_info=True,
                )

    # ------------------------------------------------------------------ commands

    def handle_sweep(self, args: str) -> str:
        """Dispatch a ``/sweep`` subcommand and return the message to display.

        Args:
            args: The text following ``/sweep`` (e.g. ``"on"``, ``"status"``).

        Returns:
            The human-readable result to show in the CLI.
        """
        verb = args.strip().split(maxsplit=1)
        head = verb[0].strip().lower() if verb else ""
        rest = verb[1].strip().lower() if len(verb) > 1 else ""

        if head in ("", "status"):
            return self._status()
        if head == "on":
            self._middleware.enabled = True
            return "Street sweeper ON - pruning dead context from every model call. Originals are recoverable with recall_swept."
        if head == "off":
            self._middleware.enabled = False
            return "Street sweeper OFF - full context is sent to the model unchanged."
        if head == "log":
            return self._log()
        if head == "aggressive":
            return self._set_aggressive(rest)
        if head == "reset":
            return self._reset()
        return "Usage: /sweep [on|off|status|log|aggressive on|off|reset]"

    def _status(self) -> str:
        """Render on/off state, mode, and both session and lifetime savings."""
        mw = self._middleware
        state = "ON" if mw.enabled else "OFF"
        mode = "aggressive (Tier 0-2)" if mw.aggressive else "conservative (Tier 0-1)"
        lines = [
            f"Street sweeper: {state} | mode: {mode} | keep_recent: {mw.keep_recent}",
            "",
            "This session:",
            "  " + mw.sweep_log.format_summary().replace("\n", "\n  "),
            "",
            self._lifetime_summary(),
        ]
        return "\n".join(lines)

    def _lifetime_summary(self) -> str:
        """Render the persistent cross-session ledger."""
        with self._lock:
            tokens = int(self._lifetime["tokens_saved"])
            dollars = self._lifetime["dollars_saved"]
            calls = int(self._lifetime["calls"])
        if tokens <= 0:
            return "Lifetime: nothing saved yet."
        return f"Lifetime (all sessions): ~{tokens:,} tokens removed over {calls:,} model calls, ~${dollars:,.4f} estimated saved."

    def _log(self) -> str:
        """Render the most recent sweep actions."""
        recent = self._middleware.sweep_log.recent
        if not recent:
            return "Street sweeper: no actions recorded yet."
        lines = ["Recent sweep actions (most recent last):"]
        for action in recent[-20:]:
            saved = max(0, action["tokens_before"] - action["tokens_after"])
            tool = action["tool_name"] or "-"
            lines.append(f"  {action['technique']:<11} {tool:<14} ~{saved} tokens")
        return "\n".join(lines)

    def _set_aggressive(self, rest: str) -> str:
        """Toggle Tier 2 (head/tail truncation of large old outputs)."""
        if rest in ("on", ""):
            self._middleware.aggressive = True
            return "Street sweeper: aggressive mode ON (Tier 0-2 - large old tool outputs are truncated to head+tail)."
        if rest == "off":
            self._middleware.aggressive = False
            return "Street sweeper: aggressive mode OFF (Tier 0-1 - only lossless cleanup + stale/duplicate stubbing)."
        return "Usage: /sweep aggressive [on|off]"

    def _reset(self) -> str:
        """Zero the session log and the persistent lifetime ledger."""
        with self._lock:
            self._lifetime = dict.fromkeys(self._LIFETIME_KEYS, 0.0)
            try:
                atomic_write_text(
                    self._ledger_path, json.dumps(self._lifetime), encoding="utf-8"
                )
            except Exception:
                logger.debug(
                    "street sweeper: could not persist ledger reset", exc_info=True
                )
        self._middleware.sweep_log = SweepLog(
            usd_per_input_token=self._middleware.sweep_log.usd_per_input_token
        )
        return "Street sweeper: session and lifetime savings counters reset to zero."
