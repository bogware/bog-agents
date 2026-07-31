"""Keep-working Stop gates (Tier-1 #3).

Grok Build lets a `Stop` hook refuse to end a turn — it feeds a reason back to
the model as a user message so the agent keeps working, turning "run the tests
before you finish" into an enforced loop (capped so it can't run forever). This
brings that primitive to bog as an `AgentMiddleware`.

A **stop check** is an injected callable run at the agent's natural stop (no
further tool calls). If any check returns a blocking `StopDecision`, its reason
is injected as a `HumanMessage` and the agent loops back to the model
(`jump_to="model"`); after `max_continuations` blocked stops the gate gives up
and lets the turn end so it can never wedge. Checks are **fail-open**: a check
that raises is logged and ignored, never blocks.

The middleware is the tested core; the CLI wires concrete checks — a
`command_stop_check` that runs the test/lint suite, or a shell/programmatic
`Stop` hook. This mirrors bog's `RubricMiddleware` (an LLM-graded stop loop);
the Stop gate is the deterministic, check-driven sibling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NotRequired

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
    hook_config,
)
from langchain_core.messages import HumanMessage
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage
    from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

STOP_GATE_MESSAGE_SOURCE = "stop_gate"
_DEFAULT_MAX_CONTINUATIONS = 8


@dataclass
class StopDecision:
    """A stop check's verdict.

    Attributes:
        block: True to refuse the turn end and loop the agent.
        reason: Why the turn can't finish yet — surfaced to the model.
    """

    block: bool
    reason: str = ""


@dataclass
class StopContext:
    """Context handed to each stop check at the agent's natural stop.

    Attributes:
        messages: The conversation so far (read-only).
        continuation: How many times the gate has already blocked this turn
            (0 on the first stop) — a check can relax or escalate on retries.
    """

    messages: Sequence[AnyMessage]
    continuation: int


# A stop check inspects the stop context and returns a blocking decision or None.
StopCheck = Callable[[StopContext], "StopDecision | None"]


class StopGateState(TypedDict):
    """Private state for the stop-gate continuation counter."""

    _stop_gate_continuations: NotRequired[int]


class StopGateMiddleware(AgentMiddleware[StopGateState, ContextT, ResponseT]):
    """Refuse to end a turn until every injected stop check is satisfied.

    Args:
        checks: Callables run at the natural stop; any blocking `StopDecision`
            loops the agent with its reason injected.
        max_continuations: Cap on blocked stops before the gate gives up and
            lets the turn end (default 8), so it can never wedge.
    """

    state_schema = StopGateState

    def __init__(self, checks: Sequence[StopCheck], *, max_continuations: int = _DEFAULT_MAX_CONTINUATIONS) -> None:
        super().__init__()
        self._checks = list(checks)
        self._max_continuations = max(1, max_continuations)

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: StopGateState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Run the stop checks at natural stop; loop back to the model if any block."""
        del runtime
        return self._evaluate([self._safe(c, ctx) for c, ctx in self._iter_checks(state)], state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: StopGateState, runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Async variant of `after_agent` — awaits any coroutine check results."""
        del runtime
        decisions: list[StopDecision | None] = []
        for check, ctx in self._iter_checks(state):
            try:
                result = check(ctx)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[misc]
            except Exception:
                logger.debug("stop check raised; ignoring (fail-open)", exc_info=True)
                result = None
            decisions.append(result)  # type: ignore[arg-type]
        return self._evaluate(decisions, state)

    @staticmethod
    def _safe(check: StopCheck, ctx: StopContext) -> StopDecision | None:
        """Run a check, swallowing any exception (fail-open — never blocks on error)."""
        try:
            return check(ctx)
        except Exception:
            logger.debug("stop check raised; ignoring (fail-open)", exc_info=True)
            return None

    def _iter_checks(self, state: StopGateState) -> list[tuple[StopCheck, StopContext]]:
        """Pair each check with a fresh `StopContext` for this stop."""
        messages = state.get("messages", []) if isinstance(state, dict) else []
        continuation = (state.get("_stop_gate_continuations", 0) if isinstance(state, dict) else 0) or 0
        ctx = StopContext(messages=messages, continuation=continuation)
        return [(check, ctx) for check in self._checks]

    def _evaluate(self, decisions: Sequence[StopDecision | None], state: StopGateState) -> dict[str, Any] | None:
        """Turn raw check decisions into a loop-back state update (or None)."""
        continuation = (state.get("_stop_gate_continuations", 0) if isinstance(state, dict) else 0) or 0
        if continuation >= self._max_continuations:
            logger.info("Stop gate reached its continuation cap (%d); letting the turn end.", self._max_continuations)
            return None
        reasons: list[str] = []
        for decision in decisions:
            if isinstance(decision, StopDecision) and decision.block:
                reasons.append(decision.reason.strip() or "A stop check is not yet satisfied.")
        if not reasons:
            return None
        body = "Do not finish yet — resolve the following before ending your turn:\n" + "\n".join(f"- {r}" for r in reasons)
        return {
            "messages": [
                HumanMessage(
                    content=body,
                    name=STOP_GATE_MESSAGE_SOURCE,
                    additional_kwargs={"lc_source": STOP_GATE_MESSAGE_SOURCE},
                )
            ],
            "_stop_gate_continuations": continuation + 1,
            "jump_to": "model",
        }


def command_stop_check(
    backend: Any,
    command: str,
    *,
    label: str | None = None,
    timeout: int = 600,
    max_output: int = 2000,
) -> StopCheck:
    """Build a stop check that runs a shell command as a "definition of done".

    Blocks the turn end when ``command`` exits non-zero (e.g. the test or lint
    suite fails), surfacing a truncated tail of its output as the reason.

    Args:
        backend: An execution backend with ``execute(command, timeout=...)``.
        command: The command to run (e.g. ``"uv run pytest -q"``).
        label: Friendly name for the reason (defaults to the command).
        timeout: Per-run timeout in seconds (suites get a generous default).
        max_output: How many characters of failing output to surface.

    Returns:
        A `StopCheck` suitable for `StopGateMiddleware`.
    """
    name = label or command

    def _check(_ctx: StopContext) -> StopDecision | None:
        result = backend.execute(command, timeout=timeout)
        if getattr(result, "exit_code", 0) == 0:
            return None
        output = getattr(result, "output", "")
        tail = output[-max_output:] if output else ""
        return StopDecision(block=True, reason=f"`{name}` did not pass (exit {result.exit_code}):\n{tail}")

    return _check


__all__ = [
    "StopCheck",
    "StopContext",
    "StopDecision",
    "StopGateMiddleware",
    "command_stop_check",
]
