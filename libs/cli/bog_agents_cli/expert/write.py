"""``/expert write`` — LLM-driven rule authoring + the ``write`` sub-tree dispatch.

Extracted from ``expert_controller.py`` during K4. The controller keeps
its public ``write`` / ``write_save`` / ``discard_proposal`` methods as
1-line delegators that call into here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bog_agents.middleware.expert_engine import (
    build_proposal as build_authoring_proposal,
    render_proposal as render_authoring_proposal,
    save_proposal as save_authoring_proposal,
)

if TYPE_CHECKING:
    from bog_agents_cli.expert_controller import ExpertController


def write(controller: ExpertController, intent: str) -> str:
    """``/expert write <intent>`` — LLM-driven rule authoring.

    Generates YAML implementing *intent*, validates + lints it,
    replays against the session's recent tool_call history so the
    user sees what the rule would have done. Stashes the proposal
    on the controller; the user follows up with
    ``/expert write save [filename]`` to commit it.
    """
    if not intent.strip():
        return (
            "Usage: /expert write <your policy in plain English>\n"
            "Example: /expert write block force-push to main"
        )
    if controller._model_factory is None:
        return (
            "Cannot author rules: no model factory configured. "
            "The CLI normally supplies one — this controller was "
            "constructed without model_factory= (test / programmatic "
            "use). Set model_factory= on ExpertController."
        )
    try:
        model = controller._model_factory()
    except Exception as exc:
        return f"Could not build authoring model: {exc}"

    history = controller.middleware.tool_call_history
    proposal = build_authoring_proposal(intent, model=model, history=history)
    controller._pending_proposal = proposal
    return render_authoring_proposal(proposal)


def write_save(controller: ExpertController, filename: str = "") -> str:
    """``/expert write save [filename]`` — commit the pending proposal.

    Writes the stashed proposal to disk under
    ``<cwd>/.bog-agents/expert_rules/`` and triggers a reload so
    the rule is live in the same session.
    """
    if controller._pending_proposal is None:
        return (
            "No pending proposal — run /expert write <intent> first."
        )
    if not controller._pending_proposal.ok_to_save:
        return (
            "Pending proposal has errors; fix them and rerun "
            "/expert write <intent>."
        )
    rules_dir = controller._working_dir / ".bog-agents" / "expert_rules"
    try:
        written = save_authoring_proposal(
            controller._pending_proposal,
            rules_dir=rules_dir,
            filename=filename or None,
        )
    except ValueError as exc:
        return f"Save failed: {exc}"
    controller._pending_proposal = None
    count, err = controller.middleware.reload()
    line = f"Saved {written} ({count} rule(s) now active)"
    if err:
        line += f"\nReload reported: {err}"
    return line


def discard_proposal(controller: ExpertController) -> str:
    """``/expert write cancel`` — drop the pending proposal without saving."""
    if controller._pending_proposal is None:
        return "No pending proposal to discard."
    controller._pending_proposal = None
    return "Discarded pending proposal."


def dispatch_write(controller: ExpertController, rest: str) -> str:
    """Handle the ``write`` sub-tree: ``write <intent>``, ``write save [name]``, ``write cancel``."""
    rest = rest.strip()
    if not rest:
        return write(controller, "")
    head, _, tail = rest.partition(" ")
    head = head.lower()
    if head == "save":
        return write_save(controller, tail.strip())
    if head in ("cancel", "discard"):
        return discard_proposal(controller)
    return write(controller, rest)


__all__ = [
    "discard_proposal",
    "dispatch_write",
    "write",
    "write_save",
]
