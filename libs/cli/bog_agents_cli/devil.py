"""`/devil` — adversarial second pass on the most recent assistant response.

Where ``/imagine`` explores breadth (N angles), ``/devil`` provides
depth-of-critique: a single adversarial subagent tears down the prior
proposal, then a synthesiser pass weighs both sides and offers a
revised position.

This is the structural opposite of a sycophantic LLM: instead of
amplifying agreement, we force the agent to argue against itself.
Best used after ``/think on`` so the critique has time to dig.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from bog_agents_cli.feature_helpers import (
    collect_transcript,
    invoke_model,
    resolve_active_model_spec,
)

logger = logging.getLogger(__name__)


DEVIL_SYSTEM_PROMPT = """\
You are a contrarian principal engineer asked to tear down the most
recent proposal in this conversation. You have no allegiance to it —
your job is to find the failure modes the proposer missed.

Walk through this checklist in your head before writing:
- What's the strongest argument AGAINST this proposal?
- What edge cases break it?
- What's the worst-case operational cost in 6 months?
- Is there a hidden assumption the proposer didn't justify?
- Is there a simpler, more boring alternative that does 80% of the job?

Produce ONE markdown document with these sections:

## The case against
3 to 5 numbered points, each with a concrete failure mode or counter-
example. Be specific — name files, scenarios, or numbers when you can.

## Hidden assumptions
Bullet list of assumptions the proposal silently relies on. Mark any
that are wrong with ❌.

## What I'd do instead
A paragraph proposing a different approach — or, if the original is
genuinely the right call, the single sentence:
"_The original proposal survives this critique._"

Hard rules:
- Never hedge with "but they're also right that...". Argue your side.
- Never invent details not in the transcript. Quote the proposer's
  exact words when refuting.
- Keep the total response under ~400 words.
"""


SYNTHESIS_SYSTEM_PROMPT = """\
You are the moderator. You have just read a proposal and a critique
of that proposal. Your job is to render a balanced verdict — NOT to
defend either side.

Produce ONE markdown document with these sections:

## Verdict
ONE sentence: which side is more right, and on what dimension?

## What the critique got right
2-4 bullet points naming the critique's strongest hits.

## What the proposal got right
2-4 bullet points naming what the critique missed or overstated.

## Revised position
One paragraph describing the proposal a thoughtful engineer would land
on after hearing both. Be concrete — name a path forward.

Hard rules:
- Never hedge with "more context needed". Decide.
- Total response under ~300 words.
- Never invent facts not in either side.
"""


@dataclass
class DevilResult:
    """Outcome of a single /devil run."""

    target_excerpt: str
    """The assistant message that was critiqued (first ~600 chars)."""

    critique: str
    synthesis: str
    elapsed_seconds: float

    def render(self) -> str:
        """Render the result as Rich-markup ready for the chat surface."""
        lines = [
            f"[bold]Devil's-advocate critique[/bold] "
            f"[dim]({self.elapsed_seconds:.1f}s)[/dim]\n",
            "**Critiquing:**",
            f"> {self.target_excerpt}",
            "",
            "---",
            "",
            "## Critique",
            "",
            self.critique,
            "",
            "---",
            "",
            "## Moderator's synthesis",
            "",
            self.synthesis,
        ]
        return "\n".join(lines)


def _excerpt(text: str, limit: int = 600) -> str:
    """Shorten ``text`` for display in the critique header."""
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def run_devil(app: object) -> DevilResult:
    """End-to-end ``/devil`` flow used by the app handler.

    Finds the most recent assistant message, asks an adversarial
    subagent to tear it apart, then runs a moderator pass that weighs
    both sides.

    Raises:
        ValueError: When there's no recent assistant message to critique.
        RuntimeError: When no active model spec is configured.
    """
    from bog_agents_cli.config import create_model_with_fallback

    transcript = collect_transcript(app, max_entries=40, max_chars=18_000)
    target = next(
        (entry.text for entry in reversed(transcript) if entry.role == "assistant"),
        "",
    )
    if not target.strip():
        msg = "no assistant message in this session to critique"
        raise ValueError(msg)

    # Also locate the user prompt that produced ``target`` so the
    # critique sees the original problem statement, not just the
    # answer. We do this by walking the transcript backward looking
    # for the last user message that came BEFORE the assistant target.
    user_context = ""
    found_target = False
    for entry in reversed(transcript):
        if not found_target and entry.role == "assistant" and entry.text == target:
            found_target = True
            continue
        if found_target and entry.role == "user":
            user_context = entry.text
            break

    spec = resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set a default"
        raise RuntimeError(msg)
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)
    model = model_result.model

    critique_prompt_parts = [
        "Original user request:",
        f"> {user_context}" if user_context else "(no recorded user prompt)",
        "",
        "Proposal under review (the assistant's last message):",
        target,
    ]
    critique_prompt = "\n".join(critique_prompt_parts)

    start = time.monotonic()
    critique = await invoke_model(
        model, DEVIL_SYSTEM_PROMPT, critique_prompt, timeout_seconds=75.0
    )

    synth_body_parts = [
        "Original user request:",
        user_context or "(no recorded user prompt)",
        "",
        "Proposal:",
        target,
        "",
        "Critique:",
        critique,
    ]
    synthesis = await invoke_model(
        model,
        SYNTHESIS_SYSTEM_PROMPT,
        "\n".join(synth_body_parts),
        timeout_seconds=60.0,
    )
    elapsed = time.monotonic() - start

    return DevilResult(
        target_excerpt=_excerpt(target),
        critique=critique,
        synthesis=synthesis,
        elapsed_seconds=elapsed,
    )
