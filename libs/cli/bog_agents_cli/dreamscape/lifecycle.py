"""Agent lifecycle state machine: Awake → Idle → Dormant → Dreaming → Imagining.

Track per-agent activity so the rest of dreamscape (dreams, imagination
injection, dashboard) has something to read. Inert when
``cfg.lifecycle.enabled`` is False — the middleware loads but every
hook is a passthrough.

The state machine is intentionally simple:

  AWAKE  ──user input / tool call──>  AWAKE
  AWAKE  ──silence for idle_secs──>   IDLE
  IDLE   ──silence for dormancy_secs──>  DORMANT
  DORMANT ──silence for dreaming_secs──>  DREAMING (triggers a dream)
  DORMANT ──user input / tool call──>  AWAKE
  DREAMING ──dream complete──>  DORMANT
  any state ──imagination injection──>  IMAGINING ──complete──>  AWAKE

The wall-clock transitions are evaluated lazily — we don't run a
background ticker, we just compute "what state should I be in?" each
time the middleware is consulted. That keeps the implementation
event-loop-friendly and makes the state purely a function of
(last_activity, now) so it's easy to test.

State is persisted to ``~/.bog-agents/agents/<id>/lifecycle.json``
so the dashboard can read it across sessions. Write is best-effort —
disk-full / permission-denied is logged and ignored.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from bog_agents_cli.dreamscape.config import (
    LifecycleConfig,
    is_emergency_disabled,
)

logger = logging.getLogger(__name__)


class LifecycleState(StrEnum):
    """The five recognised lifecycle states."""

    AWAKE = "awake"
    """Actively serving the user. The default for any running session."""

    IDLE = "idle"
    """Brief silence — no recent input, not yet 'dormant'. A 30-second
    pause while the user reads something is IDLE, not DORMANT."""

    DORMANT = "dormant"
    """Long silence — no input for ``dormancy_after_seconds``. Eligible
    for dreaming if Dreams config has ``auto_on_dormancy=True``."""

    DREAMING = "dreaming"
    """A dream pass is in flight. Transient — typically <30s wall-clock."""

    IMAGINING = "imagining"
    """An imagination-injection pass is in flight. Transient."""


@dataclass
class LifecycleSnapshot:
    """Persistent state for one agent's lifecycle.

    Stored as JSON at ``~/.bog-agents/agents/<id>/lifecycle.json`` so
    the dashboard and dream subsystems can read it without keeping
    a long-lived process around.
    """

    agent_id: str
    state: str = LifecycleState.AWAKE.value
    last_activity_at: float = 0.0
    """Unix timestamp of last user input or tool call."""

    last_dream_at: float = 0.0
    """Unix timestamp of the last completed dream (for rate limiting)."""

    imagination: float = 0.0
    """Compounding trait that grows by ``cfg.dreams.imagination_trait_increment``
    after each dream. Capped at 100.0 by the helpers in this module."""

    total_dreams: int = 0
    """Cumulative count — never decremented. Visible in /agent-state."""

    consecutive_tool_failures: int = 0
    """How many tool calls have failed back-to-back. Reset on success.
    Used by :class:`ImaginationMiddleware` to decide when to inject."""

    imagination_injections: int = 0
    imagination_injections_helped: int = 0
    """For the auto-disable heuristic: ratio of injections that led to
    a subsequent successful tool call. Below
    ``cfg.imagination.auto_disable_below_success_rate`` the middleware
    self-disables until the next dream lands."""


# Lifecycle state intentionally lives on disk, not in the LangGraph
# state cube — keeps the state cube small and dodges cross-loop
# serialisation pain when checkpointing across event loops.


# ---------------------------------------------------------------------------
# Computing transitions
# ---------------------------------------------------------------------------


def compute_state(
    snapshot: LifecycleSnapshot,
    cfg: LifecycleConfig,
    now: float | None = None,
) -> LifecycleState:
    """Compute the state implied by elapsed time since last activity.

    Pure function; no side effects. The middleware uses this to decide
    transitions on each hook invocation without keeping wall-clock
    timers running.
    """
    current_now = now if now is not None else time.time()
    # Transient states are owned by their respective subsystems —
    # we don't auto-exit DREAMING or IMAGINING based on elapsed time
    # (those are short-lived and managed by the dream / imagination
    # code that set them).
    current_state_str = snapshot.state
    if current_state_str in {
        LifecycleState.DREAMING.value,
        LifecycleState.IMAGINING.value,
    }:
        return LifecycleState(current_state_str)

    elapsed = current_now - (snapshot.last_activity_at or current_now)
    if elapsed < cfg.dormancy_after_seconds // 4:
        return LifecycleState.AWAKE
    if elapsed < cfg.dormancy_after_seconds:
        return LifecycleState.IDLE
    if elapsed < cfg.dormancy_after_seconds + cfg.dreaming_after_dormant_seconds:
        return LifecycleState.DORMANT
    # Past the dreaming threshold — the dream subsystem will pick this
    # up on its next poll and transition to DREAMING. Until then we
    # stay DORMANT-with-dream-eligible.
    return LifecycleState.DORMANT


def dream_eligible(snapshot: LifecycleSnapshot, cfg: LifecycleConfig) -> bool:
    """Whether the dream subsystem should trigger an automatic dream now."""
    if not cfg.enabled:
        return False
    now = time.time()
    elapsed = now - (snapshot.last_activity_at or now)
    if elapsed < cfg.dormancy_after_seconds + cfg.dreaming_after_dormant_seconds:
        return False
    # Rate-limit: don't dream more than once per
    # ``dreaming_after_dormant_seconds`` window.
    since_last_dream = now - snapshot.last_dream_at
    return since_last_dream >= cfg.dreaming_after_dormant_seconds


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------


def agent_state_dir(agent_id: str) -> Path:
    """Return ``~/.bog-agents/agents/<agent_id>/`` (created on demand)."""
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in agent_id)
    out = Path.home() / ".bog-agents" / "agents" / (safe or "default")
    with suppress(OSError):
        out.mkdir(parents=True, exist_ok=True)
    return out


def lifecycle_path(agent_id: str) -> Path:
    return agent_state_dir(agent_id) / "lifecycle.json"


def load_snapshot(agent_id: str) -> LifecycleSnapshot:
    """Read the on-disk snapshot or return a fresh one on miss/error."""
    target = lifecycle_path(agent_id)
    if not target.exists():
        return LifecycleSnapshot(agent_id=agent_id)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("lifecycle snapshot at %s unreadable: %s", target, exc)
        return LifecycleSnapshot(agent_id=agent_id)
    if not isinstance(data, dict):
        return LifecycleSnapshot(agent_id=agent_id)
    fresh = LifecycleSnapshot(agent_id=agent_id)
    for key, value in data.items():
        if hasattr(fresh, key):
            try:
                setattr(fresh, key, value)
            except (TypeError, ValueError):
                continue
    return fresh


def save_snapshot(snapshot: LifecycleSnapshot, *, enabled: bool = True) -> bool:
    """Persist the snapshot atomically. Returns whether the write succeeded."""
    if not enabled:
        return False
    target = lifecycle_path(snapshot.agent_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(snapshot), indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        logger.debug("could not persist lifecycle snapshot: %s", exc)
        return False
    return True


def bump_imagination(
    snapshot: LifecycleSnapshot, increment: float, *, cap: float = 100.0
) -> None:
    """Increase the imagination trait by ``increment``, clamped to ``cap``."""
    snapshot.imagination = min(cap, max(0.0, snapshot.imagination + increment))
    snapshot.total_dreams += 1
    snapshot.last_dream_at = time.time()


def record_activity(snapshot: LifecycleSnapshot) -> None:
    """Mark the snapshot as having just seen activity (returns to AWAKE)."""
    snapshot.state = LifecycleState.AWAKE.value
    snapshot.last_activity_at = time.time()
    snapshot.consecutive_tool_failures = 0


def record_tool_failure(snapshot: LifecycleSnapshot) -> None:
    snapshot.consecutive_tool_failures += 1


def record_tool_success(snapshot: LifecycleSnapshot) -> None:
    snapshot.consecutive_tool_failures = 0


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class LifecycleMiddleware(AgentMiddleware):
    """Update the on-disk lifecycle snapshot on every model call.

    Inert when ``cfg.enabled`` is False or the emergency-disable env
    var is set. Wraps every state mutation in try/except so a
    disk-write failure can't bubble back into the user's prompt path.

    Args:
        agent_id: Stable identifier for the running agent. Falls back
            to ``"default"`` when unset.
        cfg: Snapshot of the lifecycle config at middleware-build time.
    """

    def __init__(
        self,
        *,
        agent_id: str = "default",
        cfg: LifecycleConfig | None = None,
        dream_scheduler_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._agent_id = agent_id or "default"
        self._cfg = cfg or LifecycleConfig()
        self._tools: list[Any] = []
        # Optional factory that returns a started ``DreamScheduler``
        # (Phase 3 wiring). Called once on the first ``awrap_model_call``
        # so we're guaranteed a running event loop. The factory may
        # return None when prerequisites aren't met (e.g. no dream
        # model resolved); in that case we silently skip and never
        # try again for this middleware instance.
        self._dream_scheduler_factory = dream_scheduler_factory
        self._dream_scheduler_started = False

    @property
    def tools(self) -> list[Any]:
        return self._tools

    @property
    def active(self) -> bool:
        return self._cfg.enabled and not is_emergency_disabled()

    # Public so the dashboard + tests can read it without keeping a
    # ref to the middleware object.
    def current_snapshot(self) -> LifecycleSnapshot:
        snap = load_snapshot(self._agent_id)
        snap.state = compute_state(snap, self._cfg).value
        return snap

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if not self.active:
            return call_next(request)
        self._safely_record_activity()
        return call_next(request)

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self.active:
            return await call_next(request)
        self._safely_record_activity()
        self._maybe_start_dream_scheduler()
        return await call_next(request)

    def _maybe_start_dream_scheduler(self) -> None:
        """Lazy-start the dream scheduler on the first async call.

        Deferred to the async path because :func:`asyncio.create_task`
        needs a running event loop. We've now established one for
        sure (we're inside ``awrap_model_call``). The factory itself
        encodes the gating logic — whether ``auto_on_dormancy`` is
        true, whether a dream model can be resolved, etc.
        """
        if self._dream_scheduler_started:
            return
        factory = self._dream_scheduler_factory
        if factory is None:
            return
        try:
            factory()
        except Exception:
            logger.exception("LifecycleMiddleware: dream scheduler factory failed")
        finally:
            # Even on failure, mark as started so we don't repeatedly
            # retry a broken factory on every call.
            self._dream_scheduler_started = True

    def _safely_record_activity(self) -> None:
        try:
            snap = load_snapshot(self._agent_id)
            record_activity(snap)
            save_snapshot(snap, enabled=self._cfg.persist_state_to_disk)
        except Exception:
            # NEVER raise into the prompt path. The lifecycle is
            # observability — if it breaks, the agent still runs.
            logger.exception("lifecycle middleware failed to record activity")
