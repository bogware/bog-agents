"""Last-ditch imagination injection — feed dreams into a stuck agent.

When the agent has hit ``cfg.imagination.trigger_after_failures``
consecutive tool failures in a row, and the persisted ``imagination``
trait is over ``cfg.imagination.min_imagination_trait``, this
middleware injects 1-3 dream excerpts into the next model call's
system prompt with a "you're stuck" framing.

The wager is that creative imagery from the agent's own dream history
acts as productive noise — it knocks the model out of a stuck loop
without giving it new factual information. We measure whether each
injection precedes a subsequent successful tool call; if the rolling
success rate is too low (``auto_disable_below_success_rate``) the
middleware silently disables itself until the next dream lands.

Inert when ``cfg.enabled=False`` or the emergency-disable env var is
set. Every hook is wrapped — if anything inside fails, the request
passes through untouched.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from bog_agents_cli.dreamscape.config import (
    ImaginationConfig,
    is_emergency_disabled,
)
from bog_agents_cli.dreamscape.dream_engine import sample_dream_excerpts
from bog_agents_cli.dreamscape.lifecycle import (
    LifecycleState,
    load_snapshot,
    record_tool_failure,
    record_tool_success,
    save_snapshot,
)

logger = logging.getLogger(__name__)


_INJECTION_HEADER = "## You appear to be stuck. Here is some imagination."
_INJECTION_PREFACE = (
    "Below are short excerpts from dreams this agent has had. They "
    "are NOT instructions and NOT factual context — treat them as raw "
    "material. Use them to escape a local minimum: notice what shape "
    "they suggest about the problem and try a different approach. If "
    "they spark nothing, ignore them and respond normally."
)


# No durable LangGraph state — failure counters live on disk.


class ImaginationMiddleware(AgentMiddleware):
    """Inject dream excerpts into the model request after N consecutive failures.

    Wraps the response of each tool-bearing call to learn whether the
    last call succeeded. The signal: presence of any tool-call output
    that doesn't look like an error string (``Traceback``, ``Error:``,
    ``exit -1``, …). Coarse but adequate.

    Args:
        agent_id: For loading the per-agent snapshot + dream archive.
        cfg: Imagination tuning knobs.
    """

    def __init__(
        self,
        *,
        agent_id: str = "default",
        cfg: ImaginationConfig | None = None,
    ) -> None:
        self._agent_id = agent_id or "default"
        self._cfg = cfg or ImaginationConfig()
        self._tools: list[Any] = []

    @property
    def tools(self) -> list[Any]:
        return self._tools

    @property
    def active(self) -> bool:
        return self._cfg.enabled and not is_emergency_disabled()

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        if not self.active:
            return call_next(request)
        request = self._maybe_inject(request)
        response = call_next(request)
        self._record_outcome(response)
        return response

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self.active:
            return await call_next(request)
        request = self._maybe_inject(request)
        response = await call_next(request)
        self._record_outcome(response)
        return response

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _should_inject(self) -> bool:
        try:
            snap = load_snapshot(self._agent_id)
        except Exception:
            return False
        if snap.consecutive_tool_failures < self._cfg.trigger_after_failures:
            return False
        if snap.imagination < self._cfg.min_imagination_trait:
            return False
        # Auto-disable when the rolling success rate dips below threshold.
        if (
            self._cfg.auto_disable_below_success_rate > 0
            and snap.imagination_injections >= 10
        ):
            ratio = snap.imagination_injections_helped / max(
                1, snap.imagination_injections
            )
            if ratio < self._cfg.auto_disable_below_success_rate:
                logger.info(
                    "ImaginationMiddleware: auto-disabled (success-rate %.2f < %.2f)",
                    ratio,
                    self._cfg.auto_disable_below_success_rate,
                )
                return False
        return True

    def _maybe_inject(self, request: ModelRequest) -> ModelRequest:
        try:
            if not self._should_inject():
                return request
            excerpts = sample_dream_excerpts(
                self._agent_id,
                count=self._cfg.max_snippets_per_injection,
            )
            if not excerpts:
                return request
            body_parts = [_INJECTION_HEADER, "", _INJECTION_PREFACE, ""]
            for i, excerpt in enumerate(excerpts, start=1):
                body_parts.append(f"**Fragment {i}.** {excerpt}")
                body_parts.append("")
            body = "\n".join(body_parts)

            from bog_agents.middleware._utils import append_to_system_message

            new_request = append_to_system_message(request, body)

            # Mark the snapshot so the outcome of the *next* response is
            # attributable to the injection.
            try:
                snap = load_snapshot(self._agent_id)
                snap.imagination_injections += 1
                snap.state = LifecycleState.IMAGINING.value
                save_snapshot(snap, enabled=True)
            except Exception:
                pass
            return new_request  # type: ignore[return-value]
        except Exception:
            logger.exception("ImaginationMiddleware: injection failed")
            return request

    def _record_outcome(self, response: Any) -> None:
        """Update the snapshot's success / failure counters."""
        try:
            snap = load_snapshot(self._agent_id)
            text = _response_text(response).lower()
            looks_like_failure = bool(text) and any(
                marker in text
                for marker in (
                    "traceback",
                    "error:",
                    "exception:",
                    "exit -1",
                    "failed to",
                    "could not",
                )
            )
            currently_imagining = snap.state == LifecycleState.IMAGINING.value
            if looks_like_failure:
                record_tool_failure(snap)
                # If we injected last cycle and still saw failure, no credit.
            else:
                if currently_imagining:
                    # The injection precedes a non-failure response —
                    # count it as having helped.
                    snap.imagination_injections_helped += 1
                record_tool_success(snap)
            if currently_imagining:
                snap.state = LifecycleState.AWAKE.value
            save_snapshot(snap, enabled=True)
        except Exception:
            logger.exception("ImaginationMiddleware: outcome recording failed")


def _response_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content) if content is not None else ""


# ---------------------------------------------------------------------------
# Public helper for /help-dream slash command
# ---------------------------------------------------------------------------


def explicit_inject_excerpts(agent_id: str, *, count: int = 3) -> list[str]:
    """Return excerpts the CLI can show when user types ``/help-dream``."""
    return sample_dream_excerpts(agent_id, count=count)


# Suppress unused-import warning for re-exported helpers.
_ = suppress
