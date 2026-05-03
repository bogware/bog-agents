"""Pre-flight Q&A: clarify ambiguous prompts before an auto-mode run."""

from __future__ import annotations

import asyncio
import sys

from bog_agents_cli.auto_mode import (
    AutoModeSettings,
    detect_ambiguities,
    haiku_preflight_check,
)


async def run_preflight_qa(
    prompt: str,
    *,
    settings: AutoModeSettings,
    quiet: bool = False,
) -> str:
    """Run pre-flight clarification if the prompt is ambiguous.

    Asks the user clarifying questions on stdout/stdin. Appends their
    answers to the prompt so the agent has full context.

    Skipped when:
    - ``settings.preflight_clarification`` is False
    - ``quiet`` is True (non-interactive/pipe mode)
    - stdin is not a TTY

    Args:
        prompt: Original user prompt.
        settings: Auto-mode settings (provides haiku model choice).
        quiet: Suppress all output and skip clarification.

    Returns:
        Original prompt, or prompt augmented with Q&A answers.
    """
    if not settings.preflight_clarification or quiet or not sys.stdin.isatty():
        return prompt

    # Heuristic pass first (fast, no API call)
    questions = detect_ambiguities(prompt)

    # If heuristics found nothing and prompt is short, try Haiku
    if not questions and settings.haiku_eval.enabled and len(prompt.split()) < 25:
        questions = await haiku_preflight_check(prompt, model=settings.haiku_eval.model)
    elif questions and settings.haiku_eval.enabled:
        # Heuristics found something — Haiku can add up to 2 more
        extra = await haiku_preflight_check(prompt, model=settings.haiku_eval.model)
        seen = set(questions)
        for q in extra:
            if q not in seen and len(questions) < 4:
                questions.append(q)
                seen.add(q)

    if not questions:
        return prompt

    print("\n\033[1;33m[Auto Mode] Pre-flight clarification\033[0m", flush=True)  # noqa: T201
    print("A few quick questions before starting:\n", flush=True)  # noqa: T201

    answers: list[str] = []
    for i, question in enumerate(questions, 1):
        print(f"  {i}. {question}", flush=True)  # noqa: T201
        try:
            answer = (await asyncio.to_thread(input, "     → ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Skipping remaining questions]", flush=True)  # noqa: T201
            break
        if answer:
            answers.append(f"Q: {question}\nA: {answer}")

    if not answers:
        return prompt

    return prompt + "\n\nPre-flight context:\n" + "\n".join(answers)
