"""`/review-plan` (ROADMAP #69): open the plan review screen and act on the reviewer's decision.

`/review-plan` reviews the last assistant message; `/review-plan butcher
<job-id>`, `/review-plan jtbd <id>` and `/review-plan file <path>` review a
saved artifact. Approve sends the execution brief to the agent (and, for a
butcher job, writes the slice selection back into `manifest.json`); revise
sends the line-addressed revision prompt so the planner re-plans exactly what
was questioned; cancel does nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bog_agents_cli.plan_review import (
    PlanReview,
    PlanReviewResult,
    apply_slice_selection,
    load_review,
)

USAGE = "Usage: /review-plan [last] | /review-plan butcher <job-id> | /review-plan jtbd <id> | /review-plan file <path>"


def parse_target(command: str) -> tuple[str, str]:
    """`(kind, ref)` from the command tail; bare `/review-plan` means `last`."""
    tokens = command.strip().split()[1:]
    if not tokens or tokens[0] == "last":
        return "last", ""
    kind = tokens[0].lower()
    return kind, " ".join(tokens[1:])


def build_review(command: str, *, project_root: Path, last_text: str) -> PlanReview:
    """The review for the command's target (raises like `load_review`)."""
    kind, ref = parse_target(command)
    return load_review(kind, ref, project_root=project_root, fallback_text=last_text)


def decide(result: PlanReviewResult | None) -> tuple[str | None, str]:
    """`(prompt to send or None, note for the user)` for a screen result."""
    if result is None or result.action == "cancel":
        return None, "Plan review cancelled; nothing sent."
    review = result.review
    if result.action == "approve":
        note = f"Plan approved ({review.summary()})."
        if review.kind == "butcher" and review.source:
            try:
                changed = apply_slice_selection(Path(review.source), review.deselected)
                if changed:
                    note += f" {changed} slice(s) marked in {Path(review.source).name}."
            except (OSError, ValueError) as exc:
                note += f" Could not update the manifest: {exc}."
        return review.execution_brief(), note
    if result.action == "revise":
        prompt = review.revision_prompt()
        if not prompt:
            return None, "No comments staged; nothing to revise."
        return prompt, f"Revision requested ({len(review.comments)} comment(s))."
    return None, f"Unknown review action {result.action!r}."


async def run_review_plan_command(app: Any, command: str) -> None:  # noqa: ANN401 - the App
    """Body of `/review-plan`: open the screen, then send what the reviewer decided."""
    from bog_agents_cli.findings_controller import project_root
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage
    from bog_agents_cli.widgets.plan_review_screen import PlanReviewScreen

    getter = getattr(app, "_get_last_assistant_text", None)
    last_text = getter() if callable(getter) else ""
    root = project_root(app)
    try:
        review = build_review(command, project_root=root, last_text=last_text or "")
    except (FileNotFoundError, ValueError) as exc:
        await app._mount_message(ErrorMessage(f"{exc}\n{USAGE}"))
        return

    def _on_result(result: PlanReviewResult | None) -> None:
        prompt, note = decide(result)
        app.call_later(app._mount_message, AppMessage(note))
        if prompt:
            app.call_later(app._send_prompt_to_agent, prompt)

    app.push_screen(PlanReviewScreen(review), _on_result)


__all__ = ["USAGE", "build_review", "decide", "parse_target", "run_review_plan_command"]
