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

# K2: persistence file lives under the project's .bog-agents/. We
# record interval + auto_activate + a started_at marker so the next
# CLI launch can resume the watcher cleanly. Stop() clears the file.
_STATE_FILENAME = "watch-state.toml"
_STATE_SUBDIR = ".bog-agents"


def _effective_floor() -> float:
    """Return the active interval floor (production or test-overridden)."""
    return float(_min_interval_override) if _min_interval_override is not None else float(_MIN_INTERVAL_SECONDS)


def _state_path(working_dir: Path | str) -> Path:
    """Return ``<working_dir>/.bog-agents/watch-state.toml``."""
    return Path(working_dir).resolve() / _STATE_SUBDIR / _STATE_FILENAME


def save_state(
    working_dir: Path | str,
    *,
    interval_seconds: float,
    auto_activate: bool,
    agent_id: str = "default",
) -> Path:
    """Persist the watcher's start parameters atomically.

    Args:
        working_dir: Project root the watcher belongs to.
        interval_seconds: Configured cadence.
        auto_activate: Whether the watcher was started with --apply.
        agent_id: Dreamscape agent id.

    Returns:
        The path the state was written to.
    """
    target = _state_path(working_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use tomli_w so we don't need to hand-roll TOML escaping.
    import tomli_w

    from bog_agents_cli.io_utils import atomic_write_text

    payload = {
        "interval_seconds": float(interval_seconds),
        "auto_activate": bool(auto_activate),
        "agent_id": str(agent_id),
        "started_at": time.time(),
    }
    atomic_write_text(target, tomli_w.dumps(payload))
    return target


def load_state(working_dir: Path | str) -> dict[str, object] | None:
    """Read the persisted watcher state if present, else ``None``.

    Returns a dict with ``interval_seconds`` (float), ``auto_activate``
    (bool), ``agent_id`` (str), and ``started_at`` (float). Returns
    ``None`` when the file is missing or unreadable — the caller treats
    that as "no resume needed."
    """
    target = _state_path(working_dir)
    if not target.is_file():
        return None
    try:
        import tomllib

        with target.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read watcher state at %s: %s", target, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def clear_state(working_dir: Path | str) -> bool:
    """Delete the persistence file. Returns True iff one existed."""
    target = _state_path(working_dir)
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError as exc:
        logger.warning("Could not delete watcher state %s: %s", target, exc)
        return False
    return True


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
    working_dir: Path | str | None = None,
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
        # H8: if the loop crashed (not a clean stop()), clear the
        # persistence file so the next launch doesn't try to resume a
        # dead watcher. ``stop()`` already clears state on the clean
        # path; this defensive call is a no-op there but rescues the
        # case where ``_run_loop`` exited via an unhandled exception.
        # Failures here are non-fatal — best-effort cleanup.
        if working_dir is not None:
            try:
                clear_state(working_dir)
            except Exception:
                logger.debug(
                    "expert_watch: clear_state in crash-recovery path failed",
                    exc_info=True,
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
            working_dir=working_dir,
        ),
        name=f"expert-watcher-{key.name}",
    )
    _HANDLES[key] = _WatcherHandle(task=task, stats=stats, stop_event=stop_event)
    # K2: persist start parameters so the next app launch can resume.
    try:
        save_state(
            key,
            interval_seconds=interval,
            auto_activate=auto_activate,
            agent_id=agent_id,
        )
    except Exception:
        # State persistence is a nice-to-have; never let a write
        # failure block the user from starting the watcher.
        logger.exception("expert_watch.save_state failed (watcher still running)")
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
    # K2: drop the persistence file so a subsequent app launch
    # doesn't auto-resume a watcher the user explicitly stopped.
    try:
        clear_state(key)
    except Exception:
        logger.exception("expert_watch.clear_state failed on stop")
    return (True, "Stopped expert watcher.")


def reset() -> None:
    """Drop every cached watcher handle. Test-only."""
    _HANDLES.clear()


def resume_if_persisted(
    *,
    working_dir: Path | str,
    propose: ProposeFn,
    on_summary: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bool, str]:
    """Restart the watcher if a state file is present for *working_dir*.

    Called at app launch. Returns ``(resumed, message)``. ``resumed``
    is False when no state file exists OR when starting fails — the
    app should log the message at debug rather than surfacing it as a
    notification.

    Args:
        working_dir: Project root the watcher belongs to.
        propose: Same propose callable :func:`start` accepts.
        on_summary: Same per-run callback :func:`start` accepts.
    """
    state = load_state(working_dir)
    if state is None:
        return (False, "no persisted watcher state")
    try:
        interval = float(state.get("interval_seconds", _DEFAULT_INTERVAL_SECONDS))
        auto_activate = bool(state.get("auto_activate", False))
        agent_id = str(state.get("agent_id", "default")) or "default"
    except (TypeError, ValueError):
        return (False, "persisted state is malformed; ignoring")
    return start(
        working_dir=working_dir,
        propose=propose,
        interval_seconds=interval,
        agent_id=agent_id,
        auto_activate=auto_activate,
        on_summary=on_summary,
    )


__all__ = [
    "WatcherStats",
    "clear_state",
    "is_running",
    "load_state",
    "reset",
    "resume_if_persisted",
    "save_state",
    "start",
    "status",
    "stop",
]
