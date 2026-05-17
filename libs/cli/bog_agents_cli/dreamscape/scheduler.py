"""Background scheduler that fires `maybe_dream` on a poll cadence.

Phase 1 + 2 wired ``maybe_dream`` as a callable but nothing was *calling*
it — dreams only happened when something else triggered them (the
``/dream`` slash command, or a manual test driver). For true ambient
dreaming the agent needs a background task that polls dream-eligibility
and fires when the lifecycle is dormant long enough.

Design points:

* **Opt-in.** The scheduler only starts when BOTH
  ``cfg.master_enabled`` AND ``cfg.dreams.auto_on_dormancy`` AND
  ``cfg.lifecycle.enabled`` are true. Any other combination → no task,
  no resource cost.
* **Lazy start.** Started from inside an ``async`` hook so we
  guarantee a running event loop. ``LifecycleMiddleware`` triggers
  ``ensure_started`` on its first ``awrap_model_call``.
* **Singleton per agent_id.** Re-importing or re-building the agent
  won't spawn duplicate tasks; ``_GLOBAL_SCHEDULERS`` keeps one
  scheduler per agent.
* **Cheap.** Default poll interval is 60s — the scheduler wakes,
  evaluates dream-eligibility in microseconds (the gate is a pure
  function of timestamps), and only spends real time when it
  actually fires a dream. Aggressively short polls are supported
  for testing (1-2s) but cost real LLM tokens; the default is meant
  for production.
* **Cancel-safe.** ``stop()`` cancels the task and awaits it. If
  the task is mid-dream, the dream's own try/except + the
  ``maybe_dream`` cleanup handler restore the snapshot to DORMANT.
* **Observability.** Each tick logs at DEBUG; dream completion logs
  at INFO. The on-disk dream files are the durable record.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bog_agents_cli.dreamscape.config import is_emergency_disabled

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from bog_agents_cli.dreamscape.config import (
        DreamsConfig,
        LifecycleConfig,
    )

# K5: callback shape. Receives (agent_id, dream_title); return is awaited
# so the scheduler can detect blocking misbehavior, but the call itself
# is dispatched on a fire-and-forget task so a slow callback never
# delays the next dream-eligibility tick.
DreamCompleteCallback = Callable[[str, str], Awaitable[None]]

logger = logging.getLogger(__name__)


_DEFAULT_POLL_SECONDS: float = 60.0
"""Default time between dream-eligibility checks.

The poll interval is independent of the dormancy window — short polls
let us catch the transition into DREAMING quickly; long polls reduce
wakeups. 60s is fine for production; tests typically use 1-3s.
"""


@dataclass
class DreamSchedulerStats:
    """In-memory counters for observability + tests."""

    ticks: int = 0
    """Total times the polling loop has woken up."""

    dreams_fired: int = 0
    """Times ``maybe_dream`` actually produced an artifact."""

    skipped_ineligible: int = 0
    """Times ``maybe_dream`` returned None (rate-limited or not
    dormant long enough)."""

    skipped_emergency_disable: int = 0
    """Times the emergency-disable env var caused a skip."""

    errors: int = 0
    """Exceptions caught inside the polling loop."""

    started_at: float = 0.0
    """Wall-clock time the scheduler was started."""

    last_tick_at: float = 0.0
    last_dream_at: float = 0.0

    fired_titles: list[str] = field(default_factory=list)
    """Titles of every dream produced — bounded; the disk archive is
    the durable record. We keep this in-memory for tests + the live
    integration data capture."""

    completion_callbacks_dispatched: int = 0
    """K5 observability: times the ``on_dream_complete`` callback was
    fired (one per successful dream when wired)."""

    completion_callbacks_failed: int = 0
    """K5 observability: times the ``on_dream_complete`` callback
    raised an exception. Non-zero values point at a misbehaving
    proposer (or other consumer) — the scheduler itself keeps running."""


# Module-level registry so re-imports don't spawn duplicate tasks.
_GLOBAL_SCHEDULERS: dict[str, DreamScheduler] = {}


class DreamScheduler:
    """Periodic background task that fires ``maybe_dream`` per agent.

    Args:
        agent_id: Stable per-agent identifier; used both as the
            registry key and passed into ``maybe_dream`` for snapshot
            routing.
        model: A LangChain ``BaseChatModel`` used by the dream engine.
        dreams_cfg: Tuning knobs for the dream engine.
        lifecycle_cfg: Lifecycle tuning (dormancy windows).
        poll_seconds: Time between eligibility checks. Defaults to
            ``_DEFAULT_POLL_SECONDS`` (60s).
        max_fired_titles: Cap on the in-memory ``fired_titles`` list
            (the disk archive is the durable record).
    """

    def __init__(
        self,
        *,
        agent_id: str,
        model: BaseChatModel,
        dreams_cfg: DreamsConfig,
        lifecycle_cfg: LifecycleConfig,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        max_fired_titles: int = 50,
        on_dream_complete: DreamCompleteCallback | None = None,
    ) -> None:
        self._agent_id = agent_id or "default"
        self._model = model
        self._dreams_cfg = dreams_cfg
        self._lifecycle_cfg = lifecycle_cfg
        self._poll_seconds = max(0.5, poll_seconds)
        self._max_fired_titles = max(1, max_fired_titles)
        self._task: asyncio.Task[None] | None = None
        # K5: optional per-dream callback. We hold a strong ref to each
        # spawned task in ``_completion_tasks`` so the GC can't reap it
        # mid-flight; tasks self-remove on completion.
        self._on_dream_complete = on_dream_complete
        self._completion_tasks: set[asyncio.Task[None]] = set()
        self.stats = DreamSchedulerStats()

    def set_on_dream_complete(self, fn: DreamCompleteCallback | None) -> None:
        """Install / replace the dream-completion callback (K5).

        Used by the CLI when it wires the expert proposer in after
        constructing the scheduler. Pass ``None`` to clear.
        """
        self._on_dream_complete = fn

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the polling task. Idempotent.

        Must be called from inside a running asyncio event loop —
        ``LifecycleMiddleware`` does this from its first
        ``awrap_model_call``.
        """
        if self.is_running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("DreamScheduler.start called outside an event loop; deferring")
            return
        self.stats.started_at = time.time()
        self._task = loop.create_task(
            self._run(), name=f"dreamscape-scheduler-{self._agent_id}"
        )
        logger.info(
            "DreamScheduler started (agent=%s, poll=%.1fs)",
            self._agent_id,
            self._poll_seconds,
        )

    async def stop(self) -> None:
        """Cancel the polling task and wait for it to exit cleanly."""
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        logger.debug("DreamScheduler stopped (agent=%s)", self._agent_id)

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """The hot loop. Catches every exception so a misbehaving dream
        cannot crash the asyncio task.

        Restart strategy: a transient outage shouldn't permanently
        disable dreaming, but recursive self-restart (the original
        v1 behavior) was a latent stack-blow-up risk if the outage
        persisted for hours. We now restart by re-entering the outer
        ``while True`` instead — bounded stack, identical behavior.
        ``CancelledError`` still propagates so ``stop()`` works.
        """
        while True:
            try:
                while True:
                    self.stats.ticks += 1
                    self.stats.last_tick_at = time.time()
                    if is_emergency_disabled():
                        self.stats.skipped_emergency_disable += 1
                    else:
                        await self._tick_once()
                    await asyncio.sleep(self._poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "DreamScheduler loop crashed (agent=%s); restarting in 30s",
                    self._agent_id,
                )
                self.stats.errors += 1
                # Iterative restart — sleep then loop back to the outer
                # while to start a fresh inner loop.
                await asyncio.sleep(30)

    async def _tick_once(self) -> None:
        """One pass: check eligibility, optionally fire a dream."""
        # Defer the import to avoid a circular dependency between
        # scheduler.py and dream_engine.py (dream_engine pulls in
        # lifecycle.py which references some scheduler glue).
        from bog_agents_cli.dreamscape.dream_engine import maybe_dream

        try:
            artifact = await maybe_dream(
                agent_id=self._agent_id,
                model=self._model,
                dreams_cfg=self._dreams_cfg,
                lifecycle_cfg=self._lifecycle_cfg,
            )
        except Exception:
            logger.exception("DreamScheduler.tick raised (agent=%s)", self._agent_id)
            self.stats.errors += 1
            return
        if artifact is None:
            self.stats.skipped_ineligible += 1
            return
        self.stats.dreams_fired += 1
        self.stats.last_dream_at = time.time()
        if len(self.stats.fired_titles) >= self._max_fired_titles:
            # Drop oldest to keep memory bounded; disk archive is the
            # durable record.
            self.stats.fired_titles.pop(0)
        self.stats.fired_titles.append(artifact.title)
        logger.info(
            "DreamScheduler fired dream (agent=%s, title=%r)",
            self._agent_id,
            artifact.title,
        )
        self._dispatch_completion(artifact.title)

    def _dispatch_completion(self, title: str) -> None:
        """K5: fire the dream-completion callback off the hot path.

        We schedule the callback as a separate task so a slow proposer
        (the typical user of this hook) cannot delay the next dream
        eligibility check. Strong-ref'd in ``_completion_tasks`` so the
        GC can't reap it before it runs; the task removes itself on
        completion.
        """
        cb = self._on_dream_complete
        if cb is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._invoke_completion(cb, title),
            name=f"dreamscape-on-complete-{self._agent_id}",
        )
        self._completion_tasks.add(task)
        task.add_done_callback(self._completion_tasks.discard)
        self.stats.completion_callbacks_dispatched += 1

    async def _invoke_completion(
        self, cb: DreamCompleteCallback, title: str
    ) -> None:
        try:
            await cb(self._agent_id, title)
        except Exception:
            self.stats.completion_callbacks_failed += 1
            logger.exception(
                "DreamScheduler on_dream_complete callback raised "
                "(agent=%s)",
                self._agent_id,
            )


# --------------------------------------------------------------------------- #
# Registry helpers                                                            #
# --------------------------------------------------------------------------- #


def ensure_scheduler(
    *,
    agent_id: str,
    model: BaseChatModel,
    dreams_cfg: DreamsConfig,
    lifecycle_cfg: LifecycleConfig,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    on_dream_complete: DreamCompleteCallback | None = None,
) -> DreamScheduler:
    """Return the (singleton) scheduler for ``agent_id``, starting if needed.

    Idempotent. If a scheduler with this id already exists, returns
    it without re-creating. When ``on_dream_complete`` is supplied on
    a later call (typical pattern for the K5 propose-on-dream hook,
    which is installed after the scheduler is already running), it
    overwrites the existing callback in place so the user always gets
    the most recent wiring.
    """
    existing = _GLOBAL_SCHEDULERS.get(agent_id)
    if existing is not None:
        if on_dream_complete is not None:
            existing.set_on_dream_complete(on_dream_complete)
        return existing
    scheduler = DreamScheduler(
        agent_id=agent_id,
        model=model,
        dreams_cfg=dreams_cfg,
        lifecycle_cfg=lifecycle_cfg,
        poll_seconds=poll_seconds,
        on_dream_complete=on_dream_complete,
    )
    _GLOBAL_SCHEDULERS[agent_id] = scheduler
    return scheduler


def get_scheduler(agent_id: str) -> DreamScheduler | None:
    """Return the registered scheduler for ``agent_id`` or None."""
    return _GLOBAL_SCHEDULERS.get(agent_id)


async def stop_all_schedulers() -> None:
    """Cancel every registered scheduler. Useful in test teardown."""
    schedulers = list(_GLOBAL_SCHEDULERS.values())
    _GLOBAL_SCHEDULERS.clear()
    for scheduler in schedulers:
        with suppress(Exception):
            await scheduler.stop()


def clear_registry() -> None:
    """Drop the registry WITHOUT cancelling tasks. Tests only."""
    _GLOBAL_SCHEDULERS.clear()
