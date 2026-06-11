"""Jobs To Be Done — execute against the job, not the literal prompt.

People don't want a quarter-inch drill; they want a quarter-inch hole —
and usually they want the shelf to stop sagging. The JTBD workflow takes
the user's prompt and, before any work happens, uncovers the **job** the
user is hiring the agent for:

1. **Interview** — the model asks 2-4 pointed questions about the
   progress being sought, the circumstance, and what would make the
   result get "fired". The user answers in their next message (or says
   ``skip`` to let the model infer).
2. **Job Spec** — a structured artifact: job statement ("When ⟨situation⟩,
   I want ⟨motivation⟩, so I can ⟨outcome⟩"), the functional / emotional /
   social dimensions of the job, measurable desired outcomes, hiring
   criteria, constraints, and non-goals. Written to
   ``.bog-agents/jtbd/<id>/job-spec.md`` in the project.
3. **Outcome-driven execution** — the agent receives a brief built from
   the spec (not the raw prompt) and is required to close with an
   *Outcome Verification* section scoring every desired outcome.
4. **Verification on demand** — ``/jtbd verify`` re-scores the session's
   work against the active spec any time.

Pure-logic module: model calls are injected as async ``invoke``
callables; TUI wiring lives in the ``handle_jtbd_subcommand`` /
``start_jtbd_interview`` / ``consume_interview_answers`` glue, which the
thin ``app.py`` handler and the prompt seam call.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli.feature_helpers import extract_json_object

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


_SPEC_SUBDIR = Path(".bog-agents") / "jtbd"
_QUESTIONS_TIMEOUT_SECONDS = 60.0
_SPEC_TIMEOUT_SECONDS = 120.0
_VERIFY_TIMEOUT_SECONDS = 180.0
_MAX_QUESTIONS = 4


# ---------------------------------------------------------------------------
# Stage 1 — the interview
# ---------------------------------------------------------------------------


QUESTIONS_SYSTEM_PROMPT = """You are a Jobs-To-Be-Done interviewer. A user
just made a request. Before anyone works on it, you must uncover the JOB
they are hiring this work to do — the progress they are trying to make in
their circumstance, not the literal feature they typed.

Ask 2 to 4 SHORT questions. Good JTBD questions probe:

* the struggling moment ("what happened that made this worth asking today?"),
* the progress sought ("when this is done, what can you do that you can't now?"),
* the firing criteria ("what result would make you throw this away?"),
* the emotional/social stakes ("who sees the result, and what should it say about you?").

Never ask about implementation details the worker can decide. Never ask
more than 4 questions.

Reply with STRICT JSON only — no prose, no markdown fence:

{"questions": ["<question 1>", "<question 2>", ...]}
"""


def parse_questions_response(text: str) -> list[str] | None:
    """Parse the interviewer's reply into a question list (None on failure)."""
    candidate = extract_json_object(text)
    if candidate is None:
        return None
    raw = candidate.get("questions")
    if not isinstance(raw, list):
        return None
    questions = [str(q).strip() for q in raw if isinstance(q, str) and str(q).strip()]
    return questions[:_MAX_QUESTIONS] or None


@dataclass
class JTBDPending:
    """Interview state held on the app while we wait for the user's answers."""

    prompt: str
    questions: list[str]
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Stage 2 — the Job Spec
# ---------------------------------------------------------------------------


SPEC_SYSTEM_PROMPT = """You are a Jobs-To-Be-Done analyst. From the user's
original request and their interview answers, produce a Job Spec that the
executing agent will optimize for INSTEAD of the literal request wording.

Be honest: if the answers reveal the literal request is the wrong solution
for the job, the desired outcomes should describe the job, and a non-goal
should name the part of the literal request that doesn't serve it.

Desired outcomes must be MEASURABLE or at least objectively checkable —
"the user can X" / "Y no longer happens" — never vibes like "better UX".

Reply with STRICT JSON only — no prose, no markdown fence:

{"job_statement": "When <situation>, I want to <motivation>, so I can <expected outcome>.",
 "functional_job": "<the practical task>",
 "emotional_job": "<how the user wants to feel / avoid feeling>",
 "social_job": "<how the user wants to be perceived; empty string if truly none>",
 "desired_outcomes": ["<measurable outcome>", ...],
 "hiring_criteria": ["<what would make the user keep this solution>", ...],
 "constraints": ["<hard constraint>", ...],
 "non_goals": ["<explicitly out of scope>", ...]}
"""


@dataclass
class JobSpec:
    """The structured Job Spec, the contract execution is scored against."""

    job_statement: str
    functional_job: str = ""
    emotional_job: str = ""
    social_job: str = ""
    desired_outcomes: list[str] = field(default_factory=list)
    hiring_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    original_prompt: str = ""
    spec_path: Path | None = None


def parse_spec_response(text: str) -> JobSpec | None:
    """Parse the analyst's reply into a JobSpec (None on failure)."""
    candidate = extract_json_object(text)
    if candidate is None:
        return None
    statement = str(candidate.get("job_statement", "")).strip()
    outcomes_raw = candidate.get("desired_outcomes")
    outcomes = (
        [str(o).strip() for o in outcomes_raw if isinstance(o, str) and str(o).strip()]
        if isinstance(outcomes_raw, list)
        else []
    )
    if not statement or not outcomes:
        return None

    def _str_list(key: str) -> list[str]:
        raw = candidate.get(key)
        if not isinstance(raw, list):
            return []
        return [str(v).strip() for v in raw if isinstance(v, str) and str(v).strip()]

    return JobSpec(
        job_statement=statement,
        functional_job=str(candidate.get("functional_job", "")).strip(),
        emotional_job=str(candidate.get("emotional_job", "")).strip(),
        social_job=str(candidate.get("social_job", "")).strip(),
        desired_outcomes=outcomes,
        hiring_criteria=_str_list("hiring_criteria"),
        constraints=_str_list("constraints"),
        non_goals=_str_list("non_goals"),
    )


def render_spec_markdown(spec: JobSpec) -> str:
    """Render the Job Spec artifact (written to the project's jtbd dir)."""

    def _section(title: str, items: list[str]) -> str:
        if not items:
            return ""
        return f"\n## {title}\n\n" + "\n".join(f"- {i}" for i in items) + "\n"

    dims = []
    if spec.functional_job:
        dims.append(f"- **Functional:** {spec.functional_job}")
    if spec.emotional_job:
        dims.append(f"- **Emotional:** {spec.emotional_job}")
    if spec.social_job:
        dims.append(f"- **Social:** {spec.social_job}")
    dims_block = (
        ("\n## Dimensions of the job\n\n" + "\n".join(dims) + "\n") if dims else ""
    )
    return (
        "# Job Spec\n\n"
        f"> {spec.job_statement}\n"
        f"{dims_block}"
        f"{_section('Desired outcomes (the score sheet)', spec.desired_outcomes)}"
        f"{_section('Hiring criteria', spec.hiring_criteria)}"
        f"{_section('Constraints', spec.constraints)}"
        f"{_section('Non-goals', spec.non_goals)}"
        f"\n## Original request\n\n{spec.original_prompt or '(not recorded)'}\n"
    )


def write_spec(spec: JobSpec, working_dir: Path) -> Path:
    """Write the spec under ``<working_dir>/.bog-agents/jtbd/<id>/job-spec.md``."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = (
        re.sub(r"[^a-z0-9]+", "-", spec.job_statement.lower())
        .strip("-")[:32]
        .rstrip("-")
        or "job"
    )
    spec_dir = working_dir / _SPEC_SUBDIR / f"{stamp}-{slug}"
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / "job-spec.md"
    path.write_text(render_spec_markdown(spec), encoding="utf-8")
    spec.spec_path = path
    return path


# ---------------------------------------------------------------------------
# Stage 3 — the execution brief
# ---------------------------------------------------------------------------


def render_execution_brief(spec: JobSpec) -> str:
    """The message the agent actually receives, built from the spec.

    The original prompt rides along as context, but the desired outcomes
    are the contract — and the agent must close with an Outcome
    Verification section so stage 4 happens inside the same turn.
    """
    outcomes = "\n".join(
        f"{i}. {o}" for i, o in enumerate(spec.desired_outcomes, start=1)
    )
    constraints = (
        ("\nConstraints:\n" + "\n".join(f"- {c}" for c in spec.constraints))
        if spec.constraints
        else ""
    )
    non_goals = (
        (
            "\nNon-goals (do NOT do these):\n"
            + "\n".join(f"- {n}" for n in spec.non_goals)
        )
        if spec.non_goals
        else ""
    )
    return (
        "Work this Jobs-To-Be-Done brief. Optimize for the desired outcomes "
        "below — they are the contract; the original request is context, "
        "not the spec.\n\n"
        f"Job: {spec.job_statement}\n\n"
        f"Desired outcomes (the score sheet):\n{outcomes}\n"
        f"{constraints}{non_goals}\n\n"
        f"Original request (context): {spec.original_prompt}\n\n"
        "When the work is complete, end your reply with a section titled "
        "'## Outcome Verification' scoring EVERY desired outcome above as "
        "met / partial / unmet, each with one line of concrete evidence "
        "(a file, a test run, an observable behavior). Score honestly — "
        "an unmet outcome reported plainly beats a met outcome invented."
    )


# ---------------------------------------------------------------------------
# Stage 4 — verification on demand
# ---------------------------------------------------------------------------


VERIFY_SYSTEM_PROMPT = """You are a Jobs-To-Be-Done auditor. You receive a
Job Spec and a conversation transcript of the work that followed. Score
each desired outcome: met / partial / unmet, with one line of evidence
drawn from the transcript (or "no evidence in transcript"). Then give a
one-paragraph hire/fire verdict: would the user keep this solution for
this job, and what single change would most improve the score?

Format as markdown: a '## Outcome Verification' table-like list, then
'## Verdict'. Be blunt; the user wants the truth, not encouragement.
"""


async def run_verification(
    spec: JobSpec,
    transcript_markdown: str,
    *,
    invoke: Callable[[str, str], Awaitable[str]],
) -> str:
    """Score the session's work against the spec. Returns rendered markdown."""
    user_block = f"## Job Spec\n\n{render_spec_markdown(spec)}\n\n## Transcript\n\n{transcript_markdown}"
    return await invoke(VERIFY_SYSTEM_PROMPT, user_block)


# ---------------------------------------------------------------------------
# CLI wiring — interview start, answer consumption, subcommands
# ---------------------------------------------------------------------------


def _build_invoke(
    app: object, timeout_seconds: float
) -> Callable[[str, str], Awaitable[str]] | None:
    """Build an ``invoke(system, user)`` on the active model, or None."""
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
        logger.warning("JTBD model %r unavailable", spec, exc_info=True)
        return None

    async def _invoke(system: str, user: str) -> str:
        return await invoke_model(model, system, user, timeout_seconds=timeout_seconds)

    return _invoke


async def start_jtbd_interview(app: object, prompt: str) -> None:
    """Stage 1: generate interview questions and park them on the app."""
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    if not prompt.strip():
        await app._mount_message(
            ErrorMessage("Usage: /jtbd <your request> — or /jtbd status|verify|cancel")
        )  # type: ignore[attr-defined]
        return
    invoke = _build_invoke(app, _QUESTIONS_TIMEOUT_SECONDS)
    if invoke is None:
        await app._mount_message(ErrorMessage("No active model — run /model first."))  # type: ignore[attr-defined]
        return
    try:
        reply = await invoke(QUESTIONS_SYSTEM_PROMPT, prompt.strip())
        questions = parse_questions_response(reply)
    except Exception as exc:
        await app._mount_message(ErrorMessage(f"/jtbd interview failed: {exc}"))  # type: ignore[attr-defined]
        return
    if not questions:
        await app._mount_message(
            ErrorMessage(
                "Could not generate interview questions — try rephrasing the request."
            )
        )  # type: ignore[attr-defined]
        return
    app._jtbd_pending = JTBDPending(prompt=prompt.strip(), questions=questions)  # type: ignore[attr-defined]
    rendered = "\n".join(f"  {i}. {q}" for i, q in enumerate(questions, start=1))
    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            "[bold]Jobs To Be Done — quick interview[/bold]\n"
            "Before working on this, help me understand the job you're hiring it for:\n\n"
            f"{rendered}\n\n"
            "Answer in your next message (free-form, numbered, or partial — all fine), "
            "or reply [bold]skip[/bold] and I'll infer the job myself."
        )
    )


async def consume_interview_answers(app: object, message: str) -> str | None:
    """Stage 2+3: turn parked questions + answers into a spec and a brief.

    Called from the prompt seam when ``app._jtbd_pending`` is set. Returns
    the execution brief the agent should receive, or None when the user's
    message should proceed unmodified (synthesis failed, or the user
    cancelled). Always clears the pending state.
    """
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    pending = getattr(app, "_jtbd_pending", None)
    app._jtbd_pending = None  # type: ignore[attr-defined]
    if not isinstance(pending, JTBDPending):
        return None
    answer = message.strip()
    if answer.lower() in {"cancel", "abort", "stop", "nevermind", "never mind"}:
        await app._mount_message(
            AppMessage(
                "JTBD interview cancelled — your message will be handled normally."
            )
        )  # type: ignore[attr-defined]
        return None
    skipped = answer.lower() == "skip"
    invoke = _build_invoke(app, _SPEC_TIMEOUT_SECONDS)
    if invoke is None:
        await app._mount_message(
            ErrorMessage(
                "No active model for spec synthesis — proceeding with your original request."
            )
        )  # type: ignore[attr-defined]
        return pending.prompt
    questions_block = "\n".join(
        f"{i}. {q}" for i, q in enumerate(pending.questions, start=1)
    )
    answers_block = (
        "(the user skipped the interview — infer the job from the request alone)"
        if skipped
        else answer
    )
    user_block = (
        f"## Original request\n\n{pending.prompt}\n\n"
        f"## Interview questions\n\n{questions_block}\n\n"
        f"## User's answers\n\n{answers_block}"
    )
    try:
        reply = await invoke(SPEC_SYSTEM_PROMPT, user_block)
        spec = parse_spec_response(reply)
    except Exception:
        logger.warning("JTBD spec synthesis failed", exc_info=True)
        spec = None
    if spec is None:
        await app._mount_message(
            ErrorMessage(
                "Job Spec synthesis failed — proceeding with your original request as-is."
            )
        )  # type: ignore[attr-defined]
        return pending.prompt
    spec.original_prompt = pending.prompt
    working_dir = Path(getattr(app, "_cwd", Path.cwd()))
    try:
        path = write_spec(spec, working_dir)
        location = f"\n[dim]Spec written to {path}[/dim]"
    except OSError:
        logger.warning("Could not write job spec artifact", exc_info=True)
        location = ""
    app._jtbd_active_spec = spec  # type: ignore[attr-defined]
    outcomes = "\n".join(
        f"  {i}. {o}" for i, o in enumerate(spec.desired_outcomes, start=1)
    )
    await app._mount_message(  # type: ignore[attr-defined]
        AppMessage(
            f"[bold]Job Spec[/bold]\n> {spec.job_statement}\n\n"
            f"[bold]Score sheet[/bold]\n{outcomes}\n\n"
            f"Executing against the outcomes (not the literal request)…{location}"
        )
    )
    return render_execution_brief(spec)


async def handle_jtbd_subcommand(app: object, raw_arg: str) -> None:
    """Dispatch ``/jtbd <sub>``: a request (start), status, verify, cancel."""
    from bog_agents_cli.widgets.messages import AppMessage, ErrorMessage

    arg = raw_arg.strip()
    head, _, _rest = arg.partition(" ")
    head_lower = head.lower()

    if head_lower == "status":
        pending = getattr(app, "_jtbd_pending", None)
        spec = getattr(app, "_jtbd_active_spec", None)
        if isinstance(pending, JTBDPending):
            await app._mount_message(
                AppMessage(
                    f"Interview in progress ({len(pending.questions)} questions outstanding) for: {pending.prompt[:120]}"
                )
            )  # type: ignore[attr-defined]
        elif isinstance(spec, JobSpec):
            where = f" — {spec.spec_path}" if spec.spec_path else ""
            await app._mount_message(
                AppMessage(
                    f"Active Job Spec{where}\n> {spec.job_statement}\nRun [bold]/jtbd verify[/bold] to score the work so far."
                )
            )  # type: ignore[attr-defined]
        else:
            await app._mount_message(
                AppMessage(
                    "No JTBD activity this session. Start one with /jtbd <your request>."
                )
            )  # type: ignore[attr-defined]
        return

    if head_lower == "cancel":
        had = isinstance(getattr(app, "_jtbd_pending", None), JTBDPending)
        app._jtbd_pending = None  # type: ignore[attr-defined]
        await app._mount_message(
            AppMessage("Interview cancelled." if had else "Nothing to cancel.")
        )  # type: ignore[attr-defined]
        return

    if head_lower == "verify":
        spec = getattr(app, "_jtbd_active_spec", None)
        if not isinstance(spec, JobSpec):
            await app._mount_message(
                ErrorMessage(
                    "No active Job Spec — start one with /jtbd <your request>."
                )
            )  # type: ignore[attr-defined]
            return
        invoke = _build_invoke(app, _VERIFY_TIMEOUT_SECONDS)
        if invoke is None:
            await app._mount_message(
                ErrorMessage("No active model — run /model first.")
            )  # type: ignore[attr-defined]
            return
        from bog_agents_cli.feature_helpers import (
            collect_transcript,
            transcript_to_markdown,
        )

        transcript = transcript_to_markdown(collect_transcript(app))
        try:
            verdict = await run_verification(spec, transcript, invoke=invoke)
        except Exception as exc:
            await app._mount_message(ErrorMessage(f"/jtbd verify failed: {exc}"))  # type: ignore[attr-defined]
            return
        await app._mount_message(AppMessage(verdict))  # type: ignore[attr-defined]
        return

    # Anything else is the request to interview.
    await start_jtbd_interview(app, arg)
