"""``/expert watch`` — scheduled-proposer dispatcher.

Extracted from ``expert_controller.py`` during K4. These are
free-function helpers that the controller delegates to so the
watcher-specific event-loop plumbing doesn't clutter the main class.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bog_agents_cli.expert_controller import ExpertController


def dispatch_watch(controller: ExpertController, rest: str) -> str:
    """Handle ``watch``, ``watch start [interval] [--apply]``, ``watch stop``."""
    from bog_agents_cli import expert_watch

    rest = rest.strip()
    if not rest or rest.lower() == "status":
        return expert_watch.status(controller._working_dir)
    head, _, tail = rest.partition(" ")
    head = head.lower()
    if head == "stop":
        return dispatch_watch_stop(controller)
    if head == "start":
        return dispatch_watch_start(controller, tail)
    return (
        "Usage: /expert watch [status | start [interval-seconds] [--apply] | stop]"
    )


def set_watch_summary_callback(controller: ExpertController, fn: Any | None) -> None:
    """Register an async callback fired after every watcher run.

    Used by the TUI's expert handler to surface a Textual
    notification when ``/expert watch`` produces a new proposal.
    Pass ``None`` to clear.
    """
    controller._on_watch_summary = fn


def resume_watcher_if_persisted(controller: ExpertController) -> tuple[bool, str]:
    """Resume a previously-started watcher if its state file is present.

    K2: called by the TUI at launch so ``/expert watch start``
    survives across app restarts. Returns ``(resumed, message)``
    — the app should log non-resumed cases at debug rather than
    surfacing them as notifications. Requires
    ``model_factory`` to have been set (the resumed watcher needs
    to be able to build models for proposals).
    """
    if controller._model_factory is None:
        return (False, "no model factory configured — skipping watcher resume")
    from bog_agents_cli import expert_watch

    return expert_watch.resume_if_persisted(
        working_dir=controller._working_dir,
        propose=controller.propose_from_dreamscape,
        on_summary=controller._on_watch_summary,
    )


def dispatch_watch_start(controller: ExpertController, rest: str) -> str:
    """``/expert watch start [interval] [--apply]`` — kick off the proposer loop."""
    from bog_agents_cli import expert_watch

    tokens = rest.split()
    auto = False
    interval = None
    for tok in tokens:
        if tok in ("--apply", "--auto", "--activate"):
            auto = True
        else:
            try:
                interval = float(tok)
            except ValueError:
                return f"Invalid interval-seconds: {tok!r}"
    if interval is None:
        interval = expert_watch._DEFAULT_INTERVAL_SECONDS
    _started, message = expert_watch.start(
        working_dir=controller._working_dir,
        propose=controller.propose_from_dreamscape,
        interval_seconds=interval,
        auto_activate=auto,
        on_summary=controller._on_watch_summary,
    )
    return message


def dispatch_watch_stop(controller: ExpertController) -> str:
    """``/expert watch stop`` — cancel the running proposer loop."""
    from bog_agents_cli import expert_watch

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return "No running event loop — can't stop watcher cleanly."
    coro = expert_watch.stop(controller._working_dir)
    if loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        stopped, message = fut.result(timeout=5)
    else:
        stopped, message = loop.run_until_complete(coro)
    _ = stopped
    return message


__all__ = [
    "dispatch_watch",
    "dispatch_watch_start",
    "dispatch_watch_stop",
    "resume_watcher_if_persisted",
    "set_watch_summary_callback",
]
