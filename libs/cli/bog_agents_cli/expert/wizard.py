"""``/expert wizard`` — guided rule-authoring flow.

Extracted from ``expert_controller.py`` during K4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bog_agents.middleware.expert_engine import (
    menu_text as wizard_menu_text,
    render_proposal as render_authoring_proposal,
    run_wizard as run_wizard_step,
)

from bog_agents_cli.expert._helpers import _NullModel

if TYPE_CHECKING:
    from bog_agents_cli.expert_controller import ExpertController


def wizard(controller: ExpertController, args: str) -> str:
    """``/expert wizard [<category> [intent]]`` — guided rule-author flow.

    With no args, prints the category menu. With a category and an
    intent, runs the wizard step (category framing + intent → LLM
    → AuthoringProposal) and stashes the result on
    :attr:`ExpertController._pending_proposal` so the user can
    ``/expert write save`` it just like a normal ``/expert write``
    proposal.
    """
    args = args.strip()
    if not args:
        return wizard_menu_text()
    head, _, rest = args.partition(" ")
    category_key = head.lower()
    intent = rest.strip()
    # No model needed for the menu / empty-intent help paths.
    if not intent:
        run = run_wizard_step(
            category_key=category_key,
            intent="",
            model=_NullModel(),
        )
        return run.error or wizard_menu_text()
    if controller._model_factory is None:
        return (
            "Cannot run wizard: no model factory configured. "
            "Pass model_factory= to ExpertController."
        )
    try:
        model = controller._model_factory()
    except Exception as exc:
        return f"Could not build wizard model: {exc}"
    history = controller.middleware.tool_call_history
    run = run_wizard_step(
        category_key=category_key,
        intent=intent,
        model=model,
        history=history,
    )
    if run.error:
        return run.error
    if run.proposal is None:
        return f"Wizard returned no proposal for category {category_key!r}."
    controller._pending_proposal = run.proposal
    lines = [
        f"== Wizard ({run.category.title if run.category else category_key}) ==",
        f"Intent: {run.proposal.intent[:200]}",
        "",
        render_authoring_proposal(run.proposal),
    ]
    return "\n".join(lines)


__all__ = ["wizard"]
