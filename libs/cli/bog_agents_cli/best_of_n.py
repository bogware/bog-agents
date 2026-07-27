"""Best-of-N attempts with rubric auto-judge (#31).

Runs N full agent attempts on the same task in isolated git worktrees
(optionally across different models), scores each resulting **diff** with a
judge (the rubric grader), ranks them, and surfaces the winner to apply.

Unlike ``/race`` — which compares bare model completions — every attempt here
is a full agent run that actually edits files, so the comparison is over real
diffs graded against the task. Nothing in the OSS ecosystem ships this
end-to-end; bog already owns the worktree isolation, the model portfolio, and
the rubric grader, so this is orchestration over primitives we have.

The control flow (`run_best_of_n`) is pure and injectable: it takes an
`attempt_runner` (run an agent for one spec → `AttemptOutcome`) and a `judge`
(score a prompt+diff → `JudgeVerdict`), so it unit-tests without real models or
git. `build_rubric_judge` and `run_worktree_attempt` supply the real wirings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AttemptSpec:
    """One attempt's identity: a label and the model spec to run it with."""

    label: str
    model: str


@dataclass
class AttemptOutcome:
    """The result of running one full agent attempt in a worktree."""

    label: str
    model: str
    worktree: str = ""
    diff: str = ""
    output: str = ""
    error: str | None = None

    @property
    def produced_change(self) -> bool:
        """True when the attempt ran without error and left a non-empty diff."""
        return self.error is None and bool(self.diff.strip())


@dataclass
class JudgeVerdict:
    """A judge's score for one attempt's diff."""

    satisfied: bool
    score: float = 0.0
    summary: str = ""


@dataclass
class ScoredAttempt:
    """An attempt outcome paired with its judge verdict (None if unjudged)."""

    outcome: AttemptOutcome
    verdict: JudgeVerdict | None = None

    @property
    def rank_key(self) -> tuple[int, float, int]:
        """Sort key (higher is better): satisfied, then score, then produced-a-change."""
        v = self.verdict
        return (
            1 if (v is not None and v.satisfied) else 0,
            v.score if v is not None else 0.0,
            1 if self.outcome.produced_change else 0,
        )


@dataclass
class BestOfNReport:
    """The full result of a best-of-N run."""

    prompt: str
    attempts: list[ScoredAttempt] = field(default_factory=list)

    @property
    def winner(self) -> ScoredAttempt | None:
        """The highest-ranked attempt that actually produced a change, or None."""
        candidates = [a for a in self.attempts if a.outcome.produced_change]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.rank_key)

    def format_summary(self) -> str:
        """Render a human-readable ranking for the TUI."""
        lines = [f"Best-of-{len(self.attempts)} for: {self.prompt[:80]}", ""]
        ranked = sorted(self.attempts, key=lambda a: a.rank_key, reverse=True)
        win = self.winner
        for i, sa in enumerate(ranked, start=1):
            o = sa.outcome
            if o.error is not None:
                status = f"error: {o.error}"
            elif not o.produced_change:
                status = "no change produced"
            elif sa.verdict is None:
                status = "unjudged"
            else:
                mark = "satisfied" if sa.verdict.satisfied else "needs revision"
                status = f"{mark} (score {sa.verdict.score:.2f})"
            crown = " ← winner" if win is not None and sa is win else ""
            lines.append(f"{i}. {o.label} [{o.model}] — {status}{crown}")
        if win is None:
            lines.append("")
            lines.append("No attempt produced an applicable change.")
        return "\n".join(lines)


AttemptRunner = Callable[[AttemptSpec], Awaitable[AttemptOutcome]]
"""Given a spec, run a full agent attempt and return its outcome (diff + output)."""

Judge = Callable[[str, str], Awaitable[JudgeVerdict]]
"""Given (prompt, diff), score the change."""


async def run_best_of_n(
    prompt: str,
    specs: Sequence[AttemptSpec],
    *,
    attempt_runner: AttemptRunner,
    judge: Judge,
    max_concurrency: int = 3,
) -> BestOfNReport:
    """Run N attempts, judge each that changed something, and rank them.

    Attempts run concurrently under a bound; a runner that raises is captured as
    a failed `AttemptOutcome` (never aborts the batch). Only attempts that
    produced a diff are sent to the judge; a judge that raises leaves that
    attempt unjudged (it can still win on the produced-change tiebreak, but
    ranks below any judged-satisfied attempt).

    Args:
        prompt: The task all attempts work on.
        specs: The attempts to run (label + model each).
        attempt_runner: Runs one attempt → outcome.
        judge: Scores one (prompt, diff).
        max_concurrency: Max attempts (and judgements) in flight at once.

    Returns:
        A `BestOfNReport` with every scored attempt and a `winner`.
    """
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _run(spec: AttemptSpec) -> AttemptOutcome:
        async with sem:
            try:
                return await attempt_runner(spec)
            except Exception as exc:
                logger.debug("attempt %s failed", spec.label, exc_info=True)
                return AttemptOutcome(
                    label=spec.label, model=spec.model, error=str(exc)
                )

    outcomes = await asyncio.gather(*[_run(s) for s in specs])

    async def _score(outcome: AttemptOutcome) -> ScoredAttempt:
        if not outcome.produced_change:
            return ScoredAttempt(outcome=outcome, verdict=None)
        async with sem:
            try:
                verdict = await judge(prompt, outcome.diff)
            except Exception:
                logger.debug(
                    "judge failed for attempt %s", outcome.label, exc_info=True
                )
                verdict = None
        return ScoredAttempt(outcome=outcome, verdict=verdict)

    scored = await asyncio.gather(*[_score(o) for o in outcomes])
    return BestOfNReport(prompt=prompt, attempts=list(scored))


def pick_winner(report: BestOfNReport) -> ScoredAttempt | None:
    """Return the winning attempt (thin alias over `BestOfNReport.winner`)."""
    return report.winner


def build_specs(
    models: Sequence[str], *, default_model: str, n: int
) -> list[AttemptSpec]:
    """Build N attempt specs from a model lineup.

    When `models` is empty, run `default_model` `n` times (a quick best-of-N on
    one model). Otherwise cycle the lineup up to `n` attempts. Labels are made
    unique so repeated models are distinguishable.

    Args:
        models: Configured model lineup (may be empty).
        default_model: Fallback model when no lineup is configured.
        n: Number of attempts requested.

    Returns:
        Exactly `max(1, n)` specs.
    """
    n = max(1, n)
    lineup = [m for m in models if m] or [default_model]
    specs: list[AttemptSpec] = []
    for i in range(n):
        model = lineup[i % len(lineup)]
        specs.append(AttemptSpec(label=f"attempt-{i + 1}", model=model))
    return specs


def _verdict_from_grader(grader_response: Any) -> JudgeVerdict:  # noqa: ANN401 - GraderResponse duck-typed
    """Map a rubric `GraderResponse` to a `JudgeVerdict`.

    Score is the fraction of criteria the grader passed (1.0 when a grader
    returns `satisfied` with no per-criterion breakdown).
    """
    result = str(getattr(grader_response, "result", "") or "").strip().lower()
    satisfied = result == "satisfied"
    summary = str(getattr(grader_response, "summary", "") or "")
    criteria = getattr(grader_response, "criteria", None) or []
    if criteria:
        passed = sum(1 for c in criteria if bool(_get(c, "passed")))
        score = passed / len(criteria)
    else:
        score = 1.0 if satisfied else 0.0
    return JudgeVerdict(satisfied=satisfied, score=score, summary=summary)


def _get(obj: Any, key: str) -> Any:  # noqa: ANN401 - dict-or-attr access
    """Read `key` from a dict or an object attribute."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def build_rubric_judge(model: Any, *, task_context: str = "") -> Judge:  # noqa: ANN401 - langchain chat model
    """Build a judge that grades a diff with the rubric `GraderResponse` schema.

    Uses the model's structured-output surface (no full agent) to keep judging
    cheap: the grader reads the task + the proposed diff and returns a terminal
    verdict, which is mapped to a `JudgeVerdict`.

    Args:
        model: A langchain chat model.
        task_context: Optional extra rubric/criteria text to steer grading.

    Returns:
        An async `Judge` callable.
    """
    from bog_agents.middleware.rubric import GraderResponse

    grader = model.with_structured_output(GraderResponse)
    system = (
        "You are a strict code reviewer grading a proposed change against a task. "
        "Return a GraderResponse: result='satisfied' only when the diff fully and "
        "correctly accomplishes the task with no regressions; otherwise "
        "'needs_revision'. Provide a per-criterion breakdown."
    )
    if task_context:
        system += f"\n\nGrading guidance:\n{task_context}"

    async def _judge(prompt: str, diff: str) -> JudgeVerdict:
        human = f"Task:\n{prompt}\n\nProposed change (unified diff):\n```diff\n{diff}\n```\n\nGrade it."
        response = await grader.ainvoke([("system", system), ("human", human)])
        return _verdict_from_grader(response)

    return _judge


async def run_worktree_attempt(
    spec: AttemptSpec,
    prompt: str,
    *,
    repo_dir: Path,
    branch_prefix: str,
    run_agent: Callable[[AttemptSpec, Path, str], Awaitable[str]],
) -> AttemptOutcome:
    """Run one attempt in a fresh git worktree and capture its diff.

    Creates a worktree off `repo_dir`, invokes `run_agent` (which runs a full
    agent rooted at the worktree and returns its final text), then captures the
    worktree's `git diff`. The worktree is left in place for the caller to apply
    or clean up (see `cleanup_worktrees`) so a winner can be materialised.

    Args:
        spec: This attempt's label + model.
        prompt: The task.
        repo_dir: The main repository.
        branch_prefix: Prefix for the throwaway attempt branch.
        run_agent: Runs the agent in the worktree → final text.

    Returns:
        An `AttemptOutcome` (with `worktree` set for cleanup); `error` is set if
        the worktree couldn't be created or the agent raised.
    """
    from bog_agents.middleware.worktree import create_worktree

    branch = f"{branch_prefix}/{spec.label}"
    try:
        info = await asyncio.to_thread(create_worktree, repo_dir, branch)
    except Exception as exc:
        return AttemptOutcome(
            label=spec.label, model=spec.model, error=f"worktree failed: {exc}"
        )

    wt_path = Path(info.path)
    try:
        output = await run_agent(spec, wt_path, prompt)
    except Exception as exc:
        return AttemptOutcome(
            label=spec.label, model=spec.model, worktree=str(wt_path), error=str(exc)
        )

    diff = await asyncio.to_thread(_worktree_diff, wt_path)
    return AttemptOutcome(
        label=spec.label,
        model=spec.model,
        worktree=str(wt_path),
        diff=diff,
        output=output,
    )


def _worktree_diff(worktree_path: Path) -> str:
    """Capture the working-tree diff (including new files) inside a worktree."""
    import subprocess  # noqa: S404 - argv-form git only, no shell

    cwd = str(worktree_path)
    try:
        # -A stages new/untracked files so they appear in the diff; then diff --cached.
        subprocess.run(
            ["git", "add", "-A"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        result = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def cleanup_worktrees(repo_dir: Path, outcomes: Sequence[AttemptOutcome]) -> None:
    """Remove every worktree created during a best-of-N run (best-effort)."""
    from bog_agents.middleware.worktree import remove_worktree

    for outcome in outcomes:
        if not outcome.worktree:
            continue
        try:
            remove_worktree(repo_dir, Path(outcome.worktree))
        except Exception:
            logger.debug(
                "failed to remove worktree %s", outcome.worktree, exc_info=True
            )


def _final_ai_text(result: Any) -> str:  # noqa: ANN401 - langgraph result mapping
    """Extract the last AI message text from an agent `ainvoke` result."""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and getattr(msg, "type", None) == "ai":
            return content if isinstance(content, str) else str(content)
    return ""


async def run_best_of_n_session(
    prompt: str,
    *,
    n: int,
    repo_dir: Path,
    model_spec: str,
    resolve_model: Callable[[str], Any],
    models: Sequence[str] = (),
    max_concurrency: int = 2,
) -> tuple[BestOfNReport, str | None]:
    """Wire and run a full best-of-N session, returning the report + winner worktree.

    Each attempt runs a real, non-interactive, auto-approving CLI agent rooted at
    its own worktree (checkpointing off so attempts don't collide on state), and
    is graded by the rubric judge. Losing worktrees are cleaned up; the winner's
    worktree is left in place so the user can inspect/merge it.

    Args:
        prompt: The task.
        n: Number of attempts.
        repo_dir: The main repository.
        model_spec: Default model spec (also the judge model).
        resolve_model: Resolves a model spec string to a langchain chat model.
        models: Optional model lineup; when empty, `model_spec` is run `n` times.
        max_concurrency: Attempts in flight at once (full agents are heavy).

    Returns:
        ``(report, winner_worktree_path_or_None)``.
    """
    from bog_agents_cli.agent import create_cli_agent

    specs = build_specs(list(models), default_model=model_spec, n=n)

    async def _run_agent(spec: AttemptSpec, wt_path: Path, task: str) -> str:
        model = resolve_model(spec.model)
        agent, _backend = create_cli_agent(
            model,
            assistant_id=f"best-of-n-{spec.label}",
            cwd=wt_path,
            interactive=False,
            auto_approve=True,
            enable_checkpointing=False,
            enable_memory=False,
            enable_plan_mode=False,
        )
        result = await agent.ainvoke({"messages": [("human", task)]})
        return _final_ai_text(result)

    async def _runner(spec: AttemptSpec) -> AttemptOutcome:
        return await run_worktree_attempt(
            spec,
            prompt,
            repo_dir=repo_dir,
            branch_prefix="best-of-n",
            run_agent=_run_agent,
        )

    judge = build_rubric_judge(resolve_model(model_spec))
    report = await run_best_of_n(
        prompt,
        specs,
        attempt_runner=_runner,
        judge=judge,
        max_concurrency=max_concurrency,
    )

    winner = report.winner
    winner_path = winner.outcome.worktree if winner is not None else None
    # Keep the winner's worktree for inspection; remove the rest.
    losers = [
        a.outcome
        for a in report.attempts
        if a.outcome.worktree and a.outcome.worktree != winner_path
    ]
    cleanup_worktrees(repo_dir, losers)
    return report, winner_path or None
