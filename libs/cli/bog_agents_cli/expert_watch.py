"""Scheduled background proposer for /expert (Wave I).

``/expert watch start [interval]`` spawns an asyncio task that calls
:func:`bog_agents_cli.dreamscape.rule_proposer.propose_rules` every
N seconds (default 4 hours). ``/expert watch stop`` cancels it.
``/expert watch`` shows status.

Design constraints:

* The watcher runs on the running event loop — same one Textual is
  hosting — so cancellation works cleanly and proposals don't fight
  with the TUI for the thread.
* The watcher catches every exception; a transient model failure
  should not bring down the loop. A stats counter surfaces the count
  of successful + failed runs so the user can see at a glance.
* The proposer writes to the STAGING dir by default
  (``.bog-agents/expert_rules/proposals/``). The watcher exposes a
  ``--apply`` mode that auto-activates, mirroring ``/expert propose
  --apply``, but it defaults off because unattended auto-activation
  is a footgun we don't want to enable by accident.
* One watcher per cwd. Re-issuing ``/expert watch start`` while one is
  already running returns a clear "already running" message rather
  than spawning a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours
_MIN_INTERVAL_SECONDS = 60  # 1 minute floor in production
# Test escape hatch — set to 0 (or a small value) to bypass the floor.
# Lives at module level so monkeypatch can override it cheaply.
_min_interval_override: float | None = None


def _effective_floor() -> float:
    """Return the active interval floor (production or test-overridden)."""
    return float(_min_interval_override) if _min_interval_override is not None else float(_MIN_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Watcher state
# ---------------------------------------------------------------------------


@dataclass
class WatcherStats:
    """Lightweight stats surfaced via ``/expert watch``.

    Attributes:
        started_at: ``time.time()`` of last successful ``start``.
        runs: Total number of proposer invocations the loop fired.
        successes: Runs that wrote at least one proposal.
        skips: Runs that the proposer reported as ``skipped``
            (evidence too thin, or model emitted ``# no-proposals``).
        errors: Runs that raised or returned a non-skip error.
        last_run_at: ``time.time()`` of most recent fire.
        last_summary: Short human-readable note from the last run.
    """

    started_at: float = 0.0
    runs: int = 0
    successes: int = 0
    skips: int = 0
    errors: int = 0
    last_run_at: float = 0.0
    last_summary: str = ""
    interval_seconds: float = float(_DEFAULT_INTERVAL_SECONDS)
    auto_activate: bool = False


@dataclass
class _WatcherHandle:
    """Internal: one running watcher per cwd."""

    task: asyncio.Task[None]
    stats: WatcherStats = field(default_factory=WatcherStats)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Per-cwd registry
# ---------------------------------------------------------------------------


_HANDLES: dict[Path, _WatcherHandle] = {}


def _key(working_dir: Path | str) -> Path:
    return Path(working_dir).resolve()


def is_running(working_dir: Path | str) -> bool:
    """True iff a watcher is alive for this cwd."""
    h = _HANDLES.get(_key(working_dir))
    return h is not None and not h.task.done()


def status(working_dir: Path | str) -> str:
    """Plain-text status for ``/expert watch`` (no args)."""
    handle = _HANDLES.get(_key(working_dir))
    if handle is None or handle.task.done():
        return (
            "Expert watcher: not running.\n"
            "Start with /expert watch start [interval-seconds]\n"
            f"(default interval: {_DEFAULT_INTERVAL_SECONDS}s = "
            f"{_DEFAULT_INTERVAL_SECONDS // 3600}h)"
        )
    s = handle.stats
    next_run = (
        s.last_run_at + s.interval_seconds if s.last_run_at else s.started_at + s.interval_seconds
    )
    next_in = max(0, int(next_run - time.time()))
    mode = "AUTO-APPLY" if s.auto_activate else "STAGED"
    lines = [
        f"Expert watcher: RUNNING ({mode})",
        f"  interval: {s.interval_seconds:.0f}s",
        f"  runs: {s.runs}  (successes {s.successes}, skips {s.skips}, errors {s.errors})",
        f"  next run in: ~{next_in}s",
    ]
    if s.last_summary:
        lines.append(f"  last: {s.last_summary}")
    lines.append("Stop with /expert watch stop")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------


# A propose callable matches the controller's `propose_from_dreamscape`
# signature: (agent_id: str, *, auto_activate: bool) -> str.
ProposeFn = Callable[..., str]


async def _run_loop(
    *,
    stop_event: asyncio.Event,
    stats: WatcherStats,
    propose: ProposeFn,
    interval_seconds: float,
    agent_id: str,
    auto_activate: bool,
    on_summary: Callable[[str], Awaitable[None]] | None,
) -> None:
    """The asyncio task body. Stops cleanly when ``stop_event`` fires."""
    s = stats
    s.interval_seconds = interval_seconds
    s.auto_activate = auto_activate
    s.started_at = time.time()
    try:
        while not stop_event.is_set():
            try:
                # Sleep for the full interval BEFORE the first call so
                # the user has a moment to issue more commands without
                # the watcher firing immediately. They can /expert
                # propose manually if they want one now.
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=interval_seconds,
                )
                # If we get here, the stop event fired.
                break
            except TimeoutError:
                pass
            # Fire one proposer run.
            s.runs += 1
            s.last_run_at = time.time()
            try:
                summary = propose(agent_id, auto_activate=auto_activate)
                lower = summary.lower()
                if "saved proposal" in lower or "auto-activated" in lower:
                    s.successes += 1
                    headline = summary.splitlines()[0]
                elif "no patterns" in lower or "no dreams" in lower:
                    s.skips += 1
                    headline = "skipped (no patterns)"
                elif "failed" in lower or "could not" in lower:
                    s.errors += 1
                    headline = summary.splitlines()[0]
                else:
                    s.skips += 1
                    headline = summary.splitlines()[0] if summary else "(empty)"
                s.last_summary = headline[:160]
            except Exception as exc:
                s.errors += 1
                s.last_summary = f"error: {exc!s}"[:160]
                logger.exception("expert_watch: propose() raised")
            if on_summary is not None:
                try:
                    await on_summary(s.last_summary)
                except Exception:
                    logger.exception("expert_watch: on_summary raised")
    finally:
        # ``CancelledError`` propagates naturally through ``finally`` —
        # we just log the loop's final stats. The previous explicit
        # ``except CancelledError: raise`` was redundant (TRY203).
        logger.info(
            "expert_watch loop exiting (runs=%d successes=%d skips=%d errors=%d)",
            s.runs,
            s.successes,
            s.skips,
            s.errors,
        )


def start(
    *,
    working_dir: Path | str,
    propose: ProposeFn,
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    agent_id: str = "default",
    auto_activate: bool = False,
    on_summary: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """Spawn (or return existing) watcher task for *working_dir*.

    Args:
        working_dir: Per-cwd key.
        propose: Callable matching
            :meth:`ExpertController.propose_from_dreamscape`. Decoupled
            so tests can swap a stub without going through the full
            controller chain.
        interval_seconds: Floor at ``_MIN_INTERVAL_SECONDS``.
        agent_id: Dreamscape agent id passed to *propose*.
        auto_activate: When True, *propose* writes directly to the
            active rules dir (the proposer's ``--apply`` mode).
            Unattended auto-activation is risky — default is False.
        on_summary: Optional async callback fired with the per-run
            short summary string. The TUI uses this to surface a
            non-blocking notification when a proposal lands.

    Returns:
        ``(started, message)`` — ``started`` False when a watcher was
        already running.
    """
    key = _key(working_dir)
    existing = _HANDLES.get(key)
    if existing is not None and not existing.task.done():
        return (False, "Expert watcher is already running. Stop it first with /expert watch stop.")

    interval = max(_effective_floor(), float(interval_seconds))
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return (False, "No running event loop — /expert watch needs an active asyncio loop.")
    stats = WatcherStats(interval_seconds=interval, auto_activate=auto_activate)
    stop_event = asyncio.Event()
    task = loop.create_task(
        _run_loop(
            stop_event=stop_event,
            stats=stats,
            propose=propose,
            interval_seconds=interval,
            agent_id=agent_id,
            auto_activate=auto_activate,
            on_summary=on_summary,
        ),
        name=f"expert-watcher-{key.name}",
    )
    _HANDLES[key] = _WatcherHandle(task=task, stats=stats, stop_event=stop_event)
    mode = "auto-apply" if auto_activate else "staged"
    return (
        True,
        (
            f"Started expert watcher ({mode}) every {interval:.0f}s "
            f"for agent {agent_id!r}. Stop with /expert watch stop."
        ),
    )


async def stop(working_dir: Path | str) -> tuple[bool, str]:
    """Cancel the watcher for *working_dir*.

    Returns:
        ``(stopped, message)``. ``stopped`` False when no watcher was
        running.
    """
    key = _key(working_dir)
    handle = _HANDLES.pop(key, None)
    if handle is None or handle.task.done():
        return (False, "No expert watcher is running for this directory.")
    handle.stop_event.set()
    handle.task.cancel()
    try:
        await handle.task
    except (asyncio.CancelledError, Exception) as exc:
        # Cancellation is expected; other exceptions are loop bugs we
        # don't want to surface as a failed-stop. Log at debug.
        logger.debug("expert_watch.stop swallowed %s", type(exc).__name__)
    return (True, "Stopped expert watcher.")


def reset() -> None:
    """Drop every cached watcher handle. Test-only."""
    _HANDLES.clear()


__all__ = [
    "WatcherStats",
    "is_running",
    "reset",
    "start",
    "status",
    "stop",
]
