r"""Standalone dreamscape runner — daemon-style entrypoint.

Runs a single :class:`DreamScheduler` in the foreground until SIGINT/
SIGTERM. The CLI's interactive TUI starts a scheduler via the
``LifecycleMiddleware`` factory, but if you want the dream cycle to
keep running while the TUI is closed (or before the user even opens
it), this module is what you launch — typically under systemd, Windows
Task Scheduler, or the :mod:`bog_agents_daemon` service.

The state is durable on its own: snapshots and dream archives live
under ``~/.bog-agents/agents/<agent_id>/`` regardless of which process
wrote them. Killing this runner and restarting it (with the same
``agent_id``) picks up exactly where the previous run left off — the
imagination trait, last-dream timestamp, and dream archive are all
filesystem-resident.

Run it with::

    python -m bog_agents_cli.dreamscape.runner --agent-id myagent

Or as a long-running service::

    python -m bog_agents_cli.dreamscape.runner \\
        --agent-id myagent \\
        --model anthropic:claude-haiku-4-5

Exit cleanly on Ctrl-C (SIGINT) or SIGTERM. The scheduler's
``stop()`` cancels its task and persists the final snapshot before
the process exits.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


def _build_model(spec: str) -> BaseChatModel:
    """Resolve a model spec like ``provider:name`` into a chat model.

    Tries the CLI's ``create_model`` first (which understands the full
    provider matrix). Falls back to a direct ``langchain_anthropic``
    construction if create_model can't be imported (e.g. running this
    module out of a minimal install).

    Args:
        spec: Either ``"provider:model_name"`` or just ``"model_name"``.

    Returns:
        A LangChain ``BaseChatModel``.
    """
    try:
        from bog_agents_cli.config import create_model

        return create_model(spec).model
    except Exception:
        logger.warning(
            "create_model failed for spec=%r; falling back to direct ChatAnthropic",
            spec,
        )
        from langchain_anthropic import ChatAnthropic

        name = spec.split(":", 1)[-1] if ":" in spec else spec
        return ChatAnthropic(model_name=name, max_tokens=300, timeout=30, stop=None)


async def run_forever(
    *,
    agent_id: str,
    model_spec: str,
    poll_seconds: float = 60.0,
    dormancy_after_seconds: int = 1800,
    dreaming_after_dormant_seconds: int = 600,
    duration_seconds: float | None = None,
) -> int:
    """Start a :class:`DreamScheduler` and block until cancelled or duration elapses.

    Args:
        agent_id: Stable per-agent identifier. Reused across runs so the
            snapshot + dream archive persist between restarts.
        model_spec: ``provider:name`` model identifier passed to
            :func:`_build_model`.
        poll_seconds: Time between eligibility checks. 60s is the
            production default; tests often pass a smaller value.
        dormancy_after_seconds: How long since last activity before the
            agent is considered DORMANT.
        dreaming_after_dormant_seconds: How long in DORMANT before the
            agent transitions to DREAMING.
        duration_seconds: If set, the runner exits cleanly after this
            many seconds. ``None`` means "run until signal."

    Returns:
        Process exit code (0 on clean shutdown, non-zero on error).
    """
    from bog_agents_cli.dreamscape.config import DreamsConfig, LifecycleConfig
    from bog_agents_cli.dreamscape.lifecycle import (
        LifecycleSnapshot,
        load_snapshot,
        save_snapshot,
    )
    from bog_agents_cli.dreamscape.scheduler import ensure_scheduler

    model = _build_model(model_spec)

    existing = load_snapshot(agent_id)
    if existing.last_activity_at <= 0.0:
        # First run for this agent_id — backdate so the first tick sees
        # DORMANT immediately instead of AWAKE.
        seeded = LifecycleSnapshot(
            agent_id=agent_id,
            last_activity_at=time.time() - float(dormancy_after_seconds + 1),
            imagination=existing.imagination,
        )
        save_snapshot(seeded, enabled=True)
        logger.info(
            "dreamscape runner: seeded fresh snapshot for agent_id=%s", agent_id
        )
    else:
        logger.info(
            "dreamscape runner: resuming agent_id=%s (imagination=%.4f, last_dream_at=%s)",
            agent_id,
            existing.imagination,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(existing.last_dream_at))
            if existing.last_dream_at
            else "never",
        )

    dreams_cfg = DreamsConfig(auto_on_dormancy=True)
    lc_cfg = LifecycleConfig(
        enabled=True,
        dormancy_after_seconds=dormancy_after_seconds,
        dreaming_after_dormant_seconds=dreaming_after_dormant_seconds,
    )
    scheduler = ensure_scheduler(
        agent_id=agent_id,
        model=model,
        dreams_cfg=dreams_cfg,
        lifecycle_cfg=lc_cfg,
        poll_seconds=poll_seconds,
    )
    scheduler.start()
    logger.info(
        "dreamscape runner: scheduler started (agent=%s, poll=%.1fs)",
        agent_id,
        poll_seconds,
    )

    stop_event = asyncio.Event()

    def _signal_stop() -> None:
        logger.info("dreamscape runner: signal received, shutting down")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except (NotImplementedError, OSError):
            # Windows asyncio doesn't support add_signal_handler — fall
            # back to a sync handler that pokes the event from the
            # default loop thread.
            signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_signal_stop))

    try:
        if duration_seconds is None:
            await stop_event.wait()
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=duration_seconds)
            except TimeoutError:
                logger.info(
                    "dreamscape runner: duration %.1fs elapsed, shutting down",
                    duration_seconds,
                )
    finally:
        await scheduler.stop()
        final = load_snapshot(agent_id)
        logger.info(
            "dreamscape runner: stopped (agent=%s, ticks=%d, dreams_fired=%d, errors=%d, imagination=%.4f)",
            agent_id,
            scheduler.stats.ticks,
            scheduler.stats.dreams_fired,
            scheduler.stats.errors,
            final.imagination,
        )
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m bog_agents_cli.dreamscape.runner",
        description="Standalone dreamscape runner — fires dreams on a poll cadence.",
    )
    p.add_argument("--agent-id", required=True, help="Per-agent state directory key.")
    p.add_argument(
        "--model",
        default=os.environ.get("BOG_AGENTS_MODEL", "anthropic:claude-haiku-4-5"),
        help="Model spec (provider:name). Defaults to anthropic:claude-haiku-4-5.",
    )
    p.add_argument("--poll-seconds", type=float, default=60.0)
    p.add_argument("--dormancy-after-seconds", type=int, default=1800)
    p.add_argument("--dreaming-after-dormant-seconds", type=int, default=600)
    p.add_argument(
        "--duration-seconds",
        type=float,
        default=None,
        help="Exit cleanly after N seconds. Default: run until signal.",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("BOG_AGENTS_LOG_LEVEL", "INFO"),
        help="Python logging level (DEBUG, INFO, WARNING, ...).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(
        run_forever(
            agent_id=args.agent_id,
            model_spec=args.model,
            poll_seconds=args.poll_seconds,
            dormancy_after_seconds=args.dormancy_after_seconds,
            dreaming_after_dormant_seconds=args.dreaming_after_dormant_seconds,
            duration_seconds=args.duration_seconds,
        )
    )


if __name__ == "__main__":  # pragma: no cover — module entrypoint
    raise SystemExit(main())
