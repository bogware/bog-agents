"""`ask_advisor` (ROADMAP #75): one bounded question from a cheap loop to the operator's `hard` tier.

The agent running on a small or local model can ask a stronger model a single
self-contained question (design choice, tricky bug, API semantics) and gets
the answer back as a tool result. The call is bounded (question + context
size), counted (per-session cap, default 5) and priced through the session's
cost ledger when one is given. The model side is injected (`ask`), so the
bundle unit-tests without a provider.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 4_000
MAX_CONTEXT_CHARS = 12_000
ADVISOR_SYSTEM = (
    "You are the senior advisor for an autonomous coding agent that runs on a smaller model. "
    "It asks you one question at a time. Answer directly and concretely in under 300 words: state the "
    "recommendation first, then the reasoning, then any pitfalls. If the question is unanswerable from "
    "the context given, say exactly what information is missing."
)


@dataclass
class AdvisorMeter:
    """Counts and prices the advisor's answers for `/cost` and the cap."""

    max_questions: int = DEFAULT_MAX_QUESTIONS
    asked: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    history: list[tuple[str, str]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        """Questions left before the cap."""
        return max(0, self.max_questions - self.asked)


AskFn = Callable[[str, str], tuple[str, int, int]]
"""`(system prompt, user prompt) -> (answer, input tokens, output tokens)`."""


def advisor_tools_bundle(
    *,
    ask: AskFn,
    model_label: str = "",
    max_questions: int = DEFAULT_MAX_QUESTIONS,
    meter: AdvisorMeter | None = None,
    on_usage: Callable[[int, int], None] | None = None,
) -> tuple[list[BaseTool], AdvisorMeter]:
    """The `ask_advisor` tool bound to an `ask` callable; returns `(tools, meter)`."""
    state = meter or AdvisorMeter(max_questions=max_questions)

    def ask_advisor(question: str, context: str = "") -> str:
        """Ask the stronger advisor model ONE self-contained question and get its answer.

        Use it sparingly for a genuinely hard decision or a bug you are stuck on
        (design choice, subtle semantics, a failing approach), not for routine
        steps. Put everything the advisor needs in `question` and `context`
        (relevant code, the error, what you tried) — it has no access to your
        conversation or files. Calls are capped per session.
        """
        if state.remaining <= 0:
            return f"Advisor cap reached ({state.max_questions} question(s) this session); decide with what you have and say so."
        question = question.strip()[:MAX_QUESTION_CHARS]
        context = context.strip()[:MAX_CONTEXT_CHARS]
        if not question:
            return "Error: the question is empty."
        prompt = question if not context else f"{question}\n\nContext:\n{context}"
        state.asked += 1
        try:
            answer, ins, outs = ask(ADVISOR_SYSTEM, prompt)
        except Exception as exc:
            logger.warning("ask_advisor failed", exc_info=True)
            return f"Advisor unavailable ({exc}); {state.remaining} question(s) left."
        state.input_tokens += ins
        state.output_tokens += outs
        if on_usage is not None:
            try:
                on_usage(ins, outs)
            except Exception:
                logger.debug("advisor usage hook failed", exc_info=True)
        state.history.append((question, answer))
        label = f" ({model_label})" if model_label else ""
        return f"Advisor{label} — {state.remaining} question(s) left after this one:\n\n{answer.strip()}"

    return [StructuredTool.from_function(func=ask_advisor, name="ask_advisor")], state


def hard_tier_ask(
    *, resolve_model: Callable[[str], Any], tier_model: str
) -> tuple[AskFn, str]:
    """An `ask` bound to the operator's `hard` tier model; returns `(ask, model spec)`."""

    def _ask(system: str, prompt: str) -> tuple[str, int, int]:
        model = resolve_model(tier_model)
        message = model.invoke([("system", system), ("human", prompt)])
        content = getattr(message, "content", message)
        if isinstance(content, list):
            content = "".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p)
                for p in content
            )
        usage = getattr(message, "usage_metadata", None) or {}
        return (
            str(content),
            int(usage.get("input_tokens", 0) or 0),
            int(usage.get("output_tokens", 0) or 0),
        )

    return _ask, tier_model


def hard_tier_model(*, active_model: str = "") -> str | None:
    """The operator config's `hard` tier model spec, or `None` when it is the active model already."""
    try:
        from bog_agents_cli.operator_mode import load_operator_config, resolve_tiers

        spec = resolve_tiers(load_operator_config())["hard"].model
    except Exception:
        return None
    if not spec or spec == active_model:
        return None
    return spec


__all__ = [
    "ADVISOR_SYSTEM",
    "AdvisorMeter",
    "AskFn",
    "advisor_tools_bundle",
    "hard_tier_ask",
    "hard_tier_model",
]
