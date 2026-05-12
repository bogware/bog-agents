"""`/imagine N` — explore the same problem from N different angles in parallel.

Each angle is a distinct subagent invocation with a tailored system
prompt (e.g. "cautious / conservative", "bold / contrarian", "minimum
effort"). All N run concurrently via :func:`asyncio.gather`, then a
final synthesiser pass ranks them and surfaces the trade-offs.

The point is *creative breadth*, not better single-shot answers — when
you ask "how should I structure the auth subsystem?" the value is in
seeing five different shapes side by side, not in picking one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from itertools import starmap

from bog_agents_cli.feature_helpers import (
    collect_transcript,
    invoke_model,
    resolve_active_model_spec,
    transcript_to_markdown,
)

logger = logging.getLogger(__name__)


# Up to 6 distinct angles. Order matches the visual order in the
# rendered output. Each angle has a *system* prompt that biases the
# subagent's voice; the *user* prompt is identical across all of them.
ANGLES: tuple[tuple[str, str], ...] = (
    (
        "Pragmatic",
        "You are a pragmatic senior engineer. Propose the simplest "
        "approach that ships this week. Favour boring, well-trodden "
        "patterns. Name the trade-offs you accept by going simple.",
    ),
    (
        "Bold",
        "You are an ambitious staff engineer. Propose the approach "
        "with the highest ceiling — even if it's harder, more novel, "
        "or takes a quarter. Justify why the upside is worth it.",
    ),
    (
        "Contrarian",
        "You are a contrarian principal engineer who has seen too many "
        "projects fail. Propose an approach that deliberately pushes "
        "back on the assumption embedded in the question. Be candid; "
        "if the question itself is wrong, name that.",
    ),
    (
        "Minimal",
        "You are an engineer with two hours of energy left this week. "
        "Propose the absolute smallest change that meets the user's "
        "literal request. Resist scope creep. Be explicit about what "
        "you are NOT doing.",
    ),
    (
        "Long-arc",
        "You are an architect thinking three years out. Propose the "
        "approach that's best for the codebase at scale — even if "
        "today's payoff is tiny. Lean into composability and "
        "interfaces. Call out which parts buy long-term flexibility.",
    ),
    (
        "First-principles",
        "You are a systems thinker. Re-derive the problem from "
        "first principles before proposing anything. Identify the "
        "fundamental constraints, then design around those.",
    ),
)


ANGLE_USER_TEMPLATE = """\
Problem statement:

{problem}

{transcript_block}

Provide your approach in this format:

**Approach** — A one-sentence headline.

**How it works** — Two to five sentences describing the shape.

**Files / pieces touched** — Bullet list of files, modules, services.

**Why this is the right call from your perspective** — One paragraph.

**Costs** — One paragraph naming what you give up by going this way.

Keep the total response under ~300 words. No preamble, no recap of the
problem.
"""


SYNTHESIS_SYSTEM_PROMPT = """\
You are the final synthesiser. You have just received N independent
proposals from engineers working in isolation, each with a different
disposition. Your job is to render a ranked comparison so the user
can choose, NOT to invent a new proposal of your own.

Produce ONE markdown document with these sections:

## Side-by-side
A markdown table with columns:
| Angle | Approach | Cost to ship | Long-term cost |

One row per proposal. Keep each cell under 12 words.

## Ranked picks
Three ordered headings: ### 1. Best balanced choice / ### 2. Best
long-arc choice / ### 3. Best if you're short on time. Under each,
name the angle and explain the choice in 2 sentences.

## Synthesis hint
ONE paragraph naming an idea that combines two of the proposals — only
if such a combination is genuinely sensible. Otherwise write
"_The proposals are mutually exclusive; pick one._"

Hard rules:
- Never invent details not in the proposals.
- Never moralise or hedge.
- Be decisive in the ranking.
"""


@dataclass(frozen=True)
class AngleProposal:
    """One angle's response, paired with metadata for rendering."""

    angle: str
    body: str
    elapsed_seconds: float
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the angle produced a non-empty body without error."""
        return not self.error and bool(self.body)


@dataclass
class ImagineResult:
    """Aggregated /imagine output ready to render in the chat surface."""

    problem: str
    proposals: list[AngleProposal]
    synthesis: str
    total_elapsed_seconds: float

    def render(self) -> str:
        """Render the full result as Rich-friendly markdown."""
        lines: list[str] = []
        lines.append(
            f"[bold]/imagine — {len(self.proposals)} angles "
            f"({self.total_elapsed_seconds:.1f}s)[/bold]\n"
        )
        for p in self.proposals:
            if p.ok:
                lines.append(
                    f"### {p.angle}  [dim]({p.elapsed_seconds:.1f}s)[/dim]\n\n"
                    f"{p.body}\n"
                )
            else:
                lines.append(f"### {p.angle}  [red](failed)[/red]\n\n{p.error}\n")
        if self.synthesis:
            lines.append("---\n")
            lines.append(self.synthesis)
        return "\n".join(lines)


def parse_args(raw: str, transcript: str) -> tuple[int, str]:
    """Pull ``(N, problem)`` out of ``/imagine`` arguments.

    Heuristics:
      * Leading integer → ``N`` (clamped to ``[2, 6]``).
      * Remaining text → problem statement.
      * If the whole arg is empty, fall back to the most recent user
        message from the transcript.

    Raises:
        ValueError: When N can't be parsed and no transcript fallback exists.
    """
    raw = raw.strip()
    parts = raw.split(None, 1)
    n = 3
    problem = ""
    if parts:
        try:
            n = int(parts[0])
            problem = parts[1].strip() if len(parts) > 1 else ""
        except ValueError:
            problem = raw
    if not problem:
        problem = transcript.strip()
    if not problem:
        msg = (
            "no problem statement provided and no recent user message "
            "to fall back on — try /imagine 4 how should we cache PR diffs?"
        )
        raise ValueError(msg)
    n = max(2, min(n, len(ANGLES)))
    return n, problem


async def run_imagine(app: object, raw_arg: str) -> ImagineResult:
    """End-to-end ``/imagine`` flow used by the app handler.

    Raises:
        ValueError: When the prompt cannot be resolved (no argument and
            no transcript fallback).
    """
    from bog_agents_cli.config import create_model_with_fallback

    transcript = collect_transcript(app, max_entries=20, max_chars=6_000)
    last_user = next(
        (entry.text for entry in reversed(transcript) if entry.role == "user"),
        "",
    )
    n, problem = parse_args(raw_arg, last_user)

    spec = resolve_active_model_spec(app)
    if not spec:
        msg = "no active model — run /model first or set a default"
        raise ValueError(msg)
    profile = getattr(app, "_profile_override", None)
    model_result = create_model_with_fallback(spec, profile_overrides=profile)
    model = model_result.model

    # Prior conversation block is appended to each angle's user prompt
    # so subagents have shared grounding context — but kept short.
    transcript_block = (
        "Prior conversation context:\n\n" + transcript_to_markdown(transcript[-6:])
        if transcript
        else ""
    )
    user_prompt = ANGLE_USER_TEMPLATE.format(
        problem=problem, transcript_block=transcript_block
    )

    selected = ANGLES[:n]

    start = time.monotonic()

    async def call_angle(name: str, system: str) -> AngleProposal:
        angle_start = time.monotonic()
        try:
            body = await invoke_model(model, system, user_prompt, timeout_seconds=60.0)
            return AngleProposal(
                angle=name,
                body=body,
                elapsed_seconds=time.monotonic() - angle_start,
            )
        except Exception as exc:
            logger.warning("/imagine angle %s failed", name, exc_info=True)
            return AngleProposal(
                angle=name,
                body="",
                elapsed_seconds=time.monotonic() - angle_start,
                error=str(exc),
            )

    proposals = await asyncio.gather(*starmap(call_angle, selected))
    ok_proposals = [p for p in proposals if p.ok]

    synthesis = ""
    if len(ok_proposals) >= 2:
        synth_body = "\n\n".join(f"### {p.angle}\n\n{p.body}" for p in ok_proposals)
        try:
            synthesis = await invoke_model(
                model,
                SYNTHESIS_SYSTEM_PROMPT,
                f"Problem:\n{problem}\n\nProposals:\n\n{synth_body}",
                timeout_seconds=60.0,
            )
        except Exception as exc:
            logger.warning("/imagine synthesis failed", exc_info=True)
            synthesis = (
                f"[dim]Synthesis pass failed: {exc} — see proposals above.[/dim]"
            )
    elif len(ok_proposals) == 1:
        synthesis = "[dim]Only one angle succeeded — no synthesis to render.[/dim]"

    return ImagineResult(
        problem=problem,
        proposals=list(proposals),
        synthesis=synthesis,
        total_elapsed_seconds=time.monotonic() - start,
    )
