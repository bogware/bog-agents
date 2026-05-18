"""``/expert propose`` + ``/expert proposals`` flows.

Extracted from ``expert_controller.py`` during K4.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bog_agents_cli.expert_controller import ExpertController


def _proposals_dir(controller: ExpertController) -> Path:
    return controller._working_dir / ".bog-agents" / "expert_rules" / "proposals"


def _rules_dir(controller: ExpertController) -> Path:
    return controller._working_dir / ".bog-agents" / "expert_rules"


def propose_from_dreamscape(
    controller: ExpertController,
    agent_id: str = "default",
    *,
    auto_activate: bool = False,
) -> str:
    """``/expert propose [agent] [--apply]`` — mine dreams + tool history → propose rules."""
    if controller._model_factory is None:
        return (
            "Cannot propose rules: no model factory configured. "
            "Pass model_factory= to ExpertController."
        )
    try:
        model = controller._model_factory()
    except Exception as exc:
        return f"Could not build proposer model: {exc}"

    from bog_agents_cli.dreamscape.rule_proposer import (
        propose_rules as _propose,
    )

    run = _propose(
        agent_id=agent_id or "default",
        model=model,
        tool_history=controller.middleware.tool_call_history,
        existing_rules=[r.name for r in controller.middleware.engine.rules],
        proposals_dir=_proposals_dir(controller),
        rules_dir=_rules_dir(controller),
        save=True,
        auto_activate=auto_activate,
    )
    if run.error and run.proposal is None:
        return f"Propose failed: {run.error}"
    if run.skipped:
        return (
            "Dreamscape proposer found no patterns worth codifying as rules. "
            f"({run.error or 'evidence not actionable'}) — try again after more activity."
        )
    if run.saved_path is None and run.proposal is not None:
        yaml_preview = run.proposal.yaml[:400]
        return (
            "Propose generated a rule that failed validation:\n"
            f"  {run.error}\n\n"
            "Model output (first 400 chars):\n"
            f"{yaml_preview}"
        )
    if run.active:
        count, err = controller.middleware.reload()
        lines = [
            f"⚡ Auto-activated rule: {run.saved_path.name}",
            f"  → wrote to {run.saved_path}",
            f"  → {count} rule(s) now active",
        ]
        if err:
            lines.append(f"  → reload warning: {err}")
        lines.append(
            f"  → revert by removing {run.saved_path.name} and running /expert reload"
        )
        return "\n".join(lines)
    return (
        f"Saved proposal: {run.saved_path.name}\n"
        f"  → review with /expert proposals\n"
        f"  → approve with /expert proposals approve {run.saved_path.name}\n"
        f"  → or skip staging next time with: /expert propose --apply"
    )


def list_proposals(controller: ExpertController) -> str:
    """``/expert proposals`` — list the YAML proposals awaiting review."""
    from bog_agents_cli.dreamscape.rule_proposer import (
        render_proposals_list,
    )

    return render_proposals_list(_proposals_dir(controller))


def approve_proposal_file(controller: ExpertController, name: str) -> str:
    """``/expert proposals approve <name>`` — promote a proposal to active rules."""
    if not name:
        return "Usage: /expert proposals approve <filename>"
    from bog_agents_cli.dreamscape.rule_proposer import approve_proposal

    try:
        target = approve_proposal(
            proposals_dir=_proposals_dir(controller),
            rules_dir=_rules_dir(controller),
            name=name,
        )
    except ValueError as exc:
        return f"Approve failed: {exc}"
    count, err = controller.middleware.reload()
    line = f"Approved {target.name} → {target} ({count} rule(s) active)"
    if err:
        line += f"\nReload reported: {err}"
    return line


def discard_proposal_file(controller: ExpertController, name: str) -> str:
    """``/expert proposals discard <name>`` — delete a pending proposal."""
    if not name:
        return "Usage: /expert proposals discard <filename>"
    from bog_agents_cli.dreamscape.rule_proposer import (
        discard_proposal as _discard,
    )

    try:
        target = _discard(proposals_dir=_proposals_dir(controller), name=name)
    except ValueError as exc:
        return f"Discard failed: {exc}"
    return f"Discarded proposal {target.name}"


def dispatch_proposals(controller: ExpertController, rest: str) -> str:
    """Handle ``proposals``, ``proposals approve <name>``, ``proposals discard <name>``."""
    rest = rest.strip()
    if not rest:
        return list_proposals(controller)
    head, _, tail = rest.partition(" ")
    head = head.lower()
    if head == "approve":
        return approve_proposal_file(controller, tail.strip())
    if head in ("discard", "delete", "reject"):
        return discard_proposal_file(controller, tail.strip())
    return "Usage: /expert proposals [approve <name> | discard <name>]"


__all__ = [
    "approve_proposal_file",
    "discard_proposal_file",
    "dispatch_proposals",
    "list_proposals",
    "propose_from_dreamscape",
]
