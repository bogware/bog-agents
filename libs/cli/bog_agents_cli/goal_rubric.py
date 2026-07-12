"""One-shot acceptance-criteria drafting from a goal objective.

Given a goal objective, an LLM drafts a concise, testable acceptance-criteria
rubric the user can review *before* work begins. The user can accept the draft
or reject it with feedback to regenerate — a regenerate-on-feedback gate.

Like ``jtbd.py``, this is a pure-logic module: the model call is injected as an
async ``invoke(system, user) -> str`` callable, so criteria drafting is
unit-testable with a stub invoke and never needs a live model. The TUI wiring
(building the invoke on the active model, parking the pending draft on the app)
lives in the thin ``app.py`` handler.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bog_agents_cli.goal_controller import parse_rubric_lines

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_DRAFT_TIMEOUT_SECONDS = 90.0


GOAL_RUBRIC_SYSTEM_PROMPT = """You draft acceptance criteria for a coding-agent \
goal.

Return ONLY a concise markdown bullet list of criteria the user can review
before work begins. Each criterion must be concrete, testable, and framed as a
definition of done. Include criteria for tests, scope control, and
user-visible behavior when relevant. Do not start implementing the goal, and do
not add prose before or after the list.
"""


@dataclass
class RubricPending:
    """A drafted rubric parked on the app awaiting accept/regenerate.

    Attributes:
        objective: The goal objective the criteria were drafted from.
        criteria: The current proposed acceptance criteria.
        created_at: Unix timestamp the draft was produced.
    """

    objective: str
    criteria: list[str]
    created_at: float = field(default_factory=time.time)


def _build_human_prompt(
    objective: str,
    *,
    feedback: str | None = None,
    previous_criteria: list[str] | None = None,
) -> str:
    """Build the human prompt for criteria generation.

    User-controlled content is wrapped in explicit boundaries so the model
    treats it as data, not instructions.

    Args:
        objective: Goal objective to turn into criteria.
        feedback: Optional user feedback for regenerating criteria.
        previous_criteria: Optional criteria the user rejected.

    Returns:
        Prompt text.
    """
    parts = ["<goal>", objective.strip(), "</goal>"]
    if feedback and feedback.strip():
        parts.extend(
            [
                "",
                (
                    "The user rejected the previous criteria. Regenerate the "
                    "criteria entirely using this feedback; do not merely patch "
                    "the prior list."
                ),
            ]
        )
        if previous_criteria:
            parts.extend(
                [
                    "",
                    "<previous_criteria>",
                    "\n".join(f"- {c}" for c in previous_criteria),
                    "</previous_criteria>",
                ]
            )
        parts.extend(["", "<user_feedback>", feedback.strip(), "</user_feedback>"])
    return "\n".join(parts)


async def draft_criteria(
    objective: str,
    *,
    invoke: Callable[[str, str], Awaitable[str]],
    feedback: str | None = None,
    previous_criteria: list[str] | None = None,
) -> list[str]:
    """Draft (or regenerate) acceptance criteria for a goal objective.

    Args:
        objective: Goal objective to turn into criteria.
        invoke: Async ``invoke(system, user) -> str`` on the active model.
        feedback: Optional user feedback; when present the model regenerates
            the criteria from scratch (the regenerate-on-feedback gate).
        previous_criteria: The criteria the user rejected, given to the model
            as context when regenerating.

    Returns:
        The proposed criteria (empty when the model returned nothing usable).
    """
    if not objective.strip():
        return []
    user = _build_human_prompt(
        objective, feedback=feedback, previous_criteria=previous_criteria
    )
    reply = await invoke(GOAL_RUBRIC_SYSTEM_PROMPT, user)
    return parse_rubric_lines(reply or "")


def build_invoke(
    app: object, timeout_seconds: float = _DRAFT_TIMEOUT_SECONDS
) -> Callable[[str, str], Awaitable[str]] | None:
    """Build an ``invoke(system, user)`` on the app's active model, or ``None``.

    Mirrors ``jtbd._build_invoke``: resolves the active model spec and returns
    an async callable, or ``None`` when no model is configured/usable so the
    caller can surface an actionable error instead of crashing.

    Args:
        app: The live ``BogAgentsApp`` (duck-typed).
        timeout_seconds: Hard wall-clock cap for the model call.

    Returns:
        An async ``invoke`` callable, or ``None`` when no model is available.
    """
    from bog_agents_cli.config import create_model_with_fallback
    from bog_agents_cli.feature_helpers import invoke_model, resolve_active_model_spec

    spec = resolve_active_model_spec(app)
    if not spec:
        return None
    try:
        model = create_model_with_fallback(
            spec, profile_overrides=getattr(app, "_profile_override", None)
        ).model
    except Exception:
        logger.warning("Goal-rubric model %r unavailable", spec, exc_info=True)
        return None

    async def _invoke(system: str, user: str) -> str:
        return await invoke_model(model, system, user, timeout_seconds=timeout_seconds)

    return _invoke


__all__ = [
    "GOAL_RUBRIC_SYSTEM_PROMPT",
    "RubricPending",
    "build_invoke",
    "draft_criteria",
]
