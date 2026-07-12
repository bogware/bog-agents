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

# Single source of truth for model-output text extraction. The live
# langchain ``ModelResponse`` keeps its output in ``.result`` (not
# ``.content``); the shared helper in ``laws.py`` handles both that and
# the bare-``AIMessage`` fallback used in tests. Imported here so the
# imagination failure-trigger reads real model output rather than the
# always-empty ``.content`` it read before.
from bog_agents_cli.dreamscape.laws import _response_text
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

# Phase 12 alternative style. Strips the "dream" framing for use on
# technical-debugging prompts where the metaphorical wrapper is judged
# as padding. See `ImaginationConfig.injection_style="neutral"`.
_NEUTRAL_INJECTION_HEADER = "## Additional context"
_NEUTRAL_INJECTION_PREFACE = (
    "Here are a few short, unrelated observations. They are not "
    "instructions or factual context. Use them only if they help you "
    "see the problem from a different angle; otherwise ignore them."
)
_DREAM_TITLE_PREFIX = "tonight i dreamed of "


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
        # Phase 27 — in-memory cache of LLM-classified prompt domains.
        # Keyed by prompt-text hash. Persists for the lifetime of the
        # middleware instance (= the agent session).
        self._llm_prompt_cache: dict[str, str] = {}

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

    async def _llm_classify_request(self, request: ModelRequest) -> str | None:
        """Phase 27 — async LLM classification of the request's prompt.

        Returns one of ``"engineering" | "creative" | "research" |
        "general"``, or ``None`` if classification cannot proceed
        (e.g. no prompt text, no model available). Caches results
        keyed by prompt hash for the middleware's lifetime.
        """
        try:
            from langchain_core.messages import HumanMessage

            from bog_agents_cli.dreamscape.domain import (
                classify_prompt_domain_llm_async,
            )

            # Extract last human message.
            last_human: str = ""
            for msg in reversed(getattr(request, "messages", []) or []):
                if isinstance(msg, HumanMessage):
                    content = getattr(msg, "content", "")
                    if isinstance(content, str):
                        last_human = content
                    elif isinstance(content, list):
                        parts: list[str] = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(str(block.get("text", "")))
                        last_human = "\n".join(parts)
                    if last_human:
                        break
            if not last_human:
                return None

            # Cache lookup.
            cache_key = str(hash(last_human))
            cached = self._llm_prompt_cache.get(cache_key)
            if cached:
                return cached

            # Live LLM call via the request's own model.
            model = getattr(request, "model", None)
            if model is None:
                return None
            domain = await classify_prompt_domain_llm_async(last_human, model)
            self._llm_prompt_cache[cache_key] = domain
            return domain
        except Exception:  # pragma: no cover — defensive
            logger.debug(
                "ImaginationMiddleware: LLM prompt classifier failed", exc_info=True
            )
            return None

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        if not self.active:
            return await call_next(request)
        # Phase 27 — async LLM prompt classification when the knob is on.
        # The override flows into _maybe_inject and overrides both
        # wrapper-style routing and content-category routing.
        prompt_override: str | None = None
        if getattr(self._cfg, "use_llm_prompt_classifier", False):
            prompt_override = await self._llm_classify_request(request)
        request = self._maybe_inject(request, prompt_domain_override=prompt_override)
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

    def _build_injection_body(
        self, excerpts: list[str], *, style_override: str | None = None
    ) -> str:
        """Render the dream-excerpt block in the configured style.

        Two styles supported via ``ImaginationConfig.injection_style``:

        * ``"dreams"`` (default) — preserves the original "You appear
          to be stuck / Fragment N" framing. Works on creative prompts
          (Phase 11).
        * ``"neutral"`` — strips the dream framing entirely. Header
          becomes *"## Additional context"*; excerpts are labeled
          *"Observation N."* and *"Tonight I dreamed of"* prefixes
          are removed. Phase 12 ablation.

        Args:
            excerpts: Dream excerpts to render.
            style_override: Phase 17 — when set, overrides
                ``cfg.injection_style`` for this single call. Used by
                ``_maybe_inject`` when per-prompt routing detects a
                decision-shaped prompt and wants to force the dreams
                wrapper.
        """
        style = style_override or getattr(self._cfg, "injection_style", "dreams")
        if style == "neutral":
            body_parts = [_NEUTRAL_INJECTION_HEADER, "", _NEUTRAL_INJECTION_PREFACE, ""]
            label = "Observation"
            excerpts_to_use = [_strip_dream_prefix(e) for e in excerpts]
        else:
            body_parts = [_INJECTION_HEADER, "", _INJECTION_PREFACE, ""]
            label = "Fragment"
            excerpts_to_use = list(excerpts)
        for i, excerpt in enumerate(excerpts_to_use, start=1):
            body_parts.append(f"**{label} {i}.** {excerpt}")
            body_parts.append("")
        return "\n".join(body_parts)

    def _route_style_for_request(
        self,
        request: ModelRequest,
        *,
        prompt_domain_override: str | None = None,
    ) -> str | None:
        """Phase 17 — pick the wrapper style for this specific request.

        Returns ``None`` when per-prompt routing is off (caller falls
        back to ``cfg.injection_style``). Returns ``"dreams"`` or
        ``"neutral"`` when the prompt classification implies an
        override.
        """
        if not getattr(self._cfg, "use_prompt_routing", False):
            return None
        try:
            from bog_agents_cli.dreamscape.domain import recommended_injection_style

            prompt_domain = prompt_domain_override or self._classify_prompt_keyword(
                request
            )
            if not prompt_domain:
                return None
            return recommended_injection_style(prompt_domain)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover — observability path
            logger.debug(
                "ImaginationMiddleware: per-prompt routing failed", exc_info=True
            )
            return None

    def _classify_prompt_keyword(self, request: ModelRequest) -> str | None:
        """Extract the latest user prompt and run the keyword classifier.

        Returns ``None`` when no prompt is found; otherwise one of
        ``"engineering" | "creative" | "research" | "general"``.
        Pulled out into its own helper so the wrapper-style + content-
        category routing paths share the extraction logic and the
        Phase 27 LLM override can substitute its result at the call
        site.
        """
        try:
            from langchain_core.messages import HumanMessage

            from bog_agents_cli.dreamscape.domain import classify_prompt_domain

            last_human: str = ""
            for msg in reversed(getattr(request, "messages", []) or []):
                if isinstance(msg, HumanMessage):
                    content = getattr(msg, "content", "")
                    if isinstance(content, str):
                        last_human = content
                    elif isinstance(content, list):
                        parts: list[str] = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append(str(block.get("text", "")))
                        last_human = "\n".join(parts)
                    if last_human:
                        break
            if not last_human:
                return None
            return classify_prompt_domain(last_human)
        except Exception:
            return None

    def _route_content_category_for_request(
        self,
        request: ModelRequest,
        *,
        prompt_domain_override: str | None = None,
    ) -> str | None:
        """Phase 21 — pick the seed category to filter on for this request.

        Returns ``None`` when content routing is off OR no specific
        category is appropriate (caller samples the full archive).
        Returns a seed-category name (e.g. ``"engineering-craft"``,
        ``"myth"``) when the prompt's classified domain has a clear
        preferred category.

        Phase 27 — accepts ``prompt_domain_override`` (e.g. from an
        async LLM classifier) which short-circuits the keyword path.
        """
        if not getattr(self._cfg, "use_content_routing", False):
            return None
        try:
            from bog_agents_cli.dreamscape.domain import preferred_seed_categories

            prompt_domain = prompt_domain_override or self._classify_prompt_keyword(
                request
            )
            if not prompt_domain:
                return None
            prefs = preferred_seed_categories(prompt_domain)  # type: ignore[arg-type]
            if not prefs:
                return None
            return prefs[0]
        except Exception:  # pragma: no cover — defensive
            logger.debug("ImaginationMiddleware: content routing failed", exc_info=True)
            return None

    def _maybe_inject(
        self,
        request: ModelRequest,
        *,
        prompt_domain_override: str | None = None,
    ) -> ModelRequest:
        try:
            if not self._should_inject():
                return request
            # Phase 21 — try per-prompt content routing first. If it
            # yields no excerpts (the archive doesn't have a matching
            # category), fall back to the unfiltered archive so the
            # injection still fires.
            content_filter = self._route_content_category_for_request(
                request, prompt_domain_override=prompt_domain_override
            )
            excerpts = sample_dream_excerpts(
                self._agent_id,
                count=self._cfg.max_snippets_per_injection,
                category_filter=content_filter,
            )
            if not excerpts and content_filter is not None:
                # Content routing produced no matches — try the
                # unfiltered archive.
                excerpts = sample_dream_excerpts(
                    self._agent_id,
                    count=self._cfg.max_snippets_per_injection,
                )
            if not excerpts:
                return request
            style_override = self._route_style_for_request(
                request, prompt_domain_override=prompt_domain_override
            )
            body = self._build_injection_body(excerpts, style_override=style_override)

            from bog_agents.middleware._utils import append_to_system_message

            new_system = append_to_system_message(request.system_message, body)
            new_request = request.override(system_message=new_system)

            # Mark the snapshot so the outcome of the *next* response is
            # attributable to the injection.
            try:
                snap = load_snapshot(self._agent_id)
                snap.imagination_injections += 1
                snap.state = LifecycleState.IMAGINING.value
                save_snapshot(snap, enabled=True)
            except Exception:
                pass
            # Phase 25 — telemetry record of the injection. The
            # category-route + style override are useful breakdowns.
            with suppress(Exception):
                from bog_agents_cli.dreamscape.telemetry import record_event

                effective_style = style_override or getattr(
                    self._cfg, "injection_style", "dreams"
                )
                record_event(
                    self._agent_id,
                    "injection_fired",
                    metadata={
                        "injection_style": effective_style,
                        "content_category": content_filter,
                        "excerpt_count": len(excerpts),
                    },
                )
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
                    # Phase 25 — telemetry record.
                    with suppress(Exception):
                        from bog_agents_cli.dreamscape.telemetry import record_event

                        record_event(self._agent_id, "injection_helped", metadata={})
                record_tool_success(snap)
            if currently_imagining:
                snap.state = LifecycleState.AWAKE.value
            save_snapshot(snap, enabled=True)
        except Exception:
            logger.exception("ImaginationMiddleware: outcome recording failed")


def _strip_dream_prefix(excerpt: str) -> str:
    """Strip a leading ``"Tonight I dreamed of "`` from a dream excerpt.

    The dream engine writes titles with this literal prefix; the
    *"neutral"* injection style removes it so the injected content
    reads as plain observation rather than a dream report. Conservative
    — only strips when the prefix is unambiguous and at the start.
    """
    stripped = excerpt.lstrip()
    lower = stripped.lower()
    if lower.startswith(_DREAM_TITLE_PREFIX):
        return stripped[len(_DREAM_TITLE_PREFIX) :].lstrip()
    return excerpt


# ---------------------------------------------------------------------------
# Public helper for /help-dream slash command
# ---------------------------------------------------------------------------


def explicit_inject_excerpts(agent_id: str, *, count: int = 3) -> list[str]:
    """Return excerpts the CLI can show when user types ``/help-dream``."""
    return sample_dream_excerpts(agent_id, count=count)


# Suppress unused-import warning for re-exported helpers.
_ = suppress
