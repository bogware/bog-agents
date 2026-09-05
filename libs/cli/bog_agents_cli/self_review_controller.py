"""Self-review-my-own-diff gate (ROADMAP killer #3).

Before a change goes to a human, the agent should run the same reviewers it
already owns against *its own* diff and decide ship-vs-fix. bog-agents is the
only agent with five composable reviewer middleware (code review, security
audit, rubric grading, hallucination detection, fact check) — so the moat here
is that nobody else can fan five independent lenses over a diff for free.

This module is pure logic (diff target parsing + prompt construction) so it is
testable without the TUI; the thin `/self-review` handler and the
`bog-agents self-review` headless subcommand both delegate here, matching the
controller-delegation convention (see `review_command.py`).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from bog_agents_cli.self_review_memo import effort_rule, normalize_effort

# The five review lenses, each mapped to the reviewer middleware whose concern it
# encodes. Fanning all five over one diff is the differentiator.
REVIEW_LENSES: list[tuple[str, str]] = [
    (
        "Correctness & bugs",
        "logic errors, edge cases, off-by-one, race conditions, error handling, "
        "regressions vs the surrounding code (CodeReviewMiddleware).",
    ),
    (
        "Security",
        "injection, auth/authorization gaps, secret/data exposure, unsafe "
        "deserialization, path traversal, SSRF (SecurityAuditMiddleware).",
    ),
    (
        "Maintainability & duplication",
        "naming, dead code, copy-paste, leaky abstractions, missing docstrings, "
        "violations of the repo's own conventions (RubricMiddleware).",
    ),
    (
        "Test coverage",
        "new/changed behavior without tests, untested edge cases, missing "
        "regression guards for the bug being fixed.",
    ),
    (
        "Over-claims & accuracy",
        "comments/docs/commit text that claim more than the code does, "
        "references to non-existent symbols/flags, unverifiable assertions "
        "(HallucinationDetectionMiddleware + FactCheckMiddleware).",
    ),
]


@dataclass
class SelfReviewTarget:
    """What the self-review gate inspects.

    Attributes:
        scope: One of ``working`` (uncommitted working-tree + staged changes —
            the default "my own diff"), ``staged`` (staged only), ``branch``
            (this branch vs a base ref), or ``commit`` (a single commit).
        ref: The base ref (for ``branch``) or commit-ish (for ``commit``).
        fix: When True, the agent must fix every blocker/critical finding in
            place and re-review until the verdict is SHIP (bounded loop).
    """

    scope: str = "working"
    ref: str = ""
    fix: bool = False
    since_last: bool = False
    effort: str = "default"


def parse_self_review_args(args: str) -> SelfReviewTarget:
    """Parse ``/self-review`` arguments.

    Supports::

        /self-review                 # uncommitted working-tree + staged changes
        /self-review --staged        # staged changes only
        /self-review --fix           # review then fix blockers and re-review
        /self-review --branch main   # this branch vs main
        /self-review HEAD~1          # a single commit

    Args:
        args: The argument string after ``/self-review``.

    Returns:
        A :class:`SelfReviewTarget`.
    """
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    fix = False
    since_last = False
    effort = "default"
    branch_ref = ""
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--fix":
            fix = True
        elif tok == "--since-last":
            since_last = True
        elif tok == "--effort" and i + 1 < len(tokens):
            effort = normalize_effort(tokens[i + 1])
            i += 1
        elif tok.startswith("--effort="):
            effort = normalize_effort(tok.split("=", 1)[1])
        elif tok == "--staged":
            rest.append("__staged__")
        elif tok == "--branch" and i + 1 < len(tokens):
            branch_ref = tokens[i + 1]
            i += 1
        else:
            rest.append(tok)
        i += 1
    common: dict[str, Any] = {"fix": fix, "since_last": since_last, "effort": effort}
    if branch_ref:
        return SelfReviewTarget(scope="branch", ref=branch_ref, **common)
    if "__staged__" in rest:
        return SelfReviewTarget(scope="staged", **common)
    concrete = [t for t in rest if t != "__staged__"]
    if concrete:
        # A bare ref like HEAD~1 / a sha -> single-commit review.
        return SelfReviewTarget(scope="commit", ref=concrete[0], **common)
    return SelfReviewTarget(scope="working", **common)


def _diff_instruction(target: SelfReviewTarget) -> str:
    """Return the git command the agent should use to obtain the diff."""
    if target.scope == "staged":
        return "the staged changes (`git diff --cached`)"
    if target.scope == "branch":
        base = target.ref or "main"
        return f"this branch's changes vs `{base}` (`git diff {base}...HEAD`)"
    if target.scope == "commit":
        return f"commit `{target.ref}` (`git show {target.ref}`)"
    # working (default): everything not yet committed.
    return (
        "all uncommitted changes — staged and unstaged "
        "(`git diff HEAD` plus any new untracked files)"
    )


def generate_self_review_prompt(target: SelfReviewTarget, *, lessons: str = "") -> str:
    """Build the multi-lens self-review prompt for the agent.

    Args:
        target: What to review and whether to fix.
        lessons: Optional block of earlier rulings (`self_review_memo.lessons_block`) appended to the prompt.

    Returns:
        The prompt string sent to the agent.
    """
    lines = [
        "# Self-Review Gate (pre-submit)",
        "",
        "You are reviewing YOUR OWN changes before they go to a human. Be your "
        "harshest critic — it is far cheaper to catch this now than in review.",
        "",
        f"First, obtain the diff to review: {_diff_instruction(target)}. "
        "Read the changed files for context as needed.",
        "",
        "Then evaluate the diff through ALL FIVE of these independent lenses. "
        "Treat each lens separately — do not let a clean pass on one excuse a "
        "skip on another:",
        "",
    ]
    for idx, (name, detail) in enumerate(REVIEW_LENSES, start=1):
        lines.append(f"{idx}. **{name}** — {detail}")
    lines.extend(
        [
            "",
            "## Output format",
            "",
            "For each lens, list findings as:",
            "- `[SEVERITY] file:line — finding` where SEVERITY is "
            "**blocker** / **warning** / **nit**.",
            "If a lens is clean, say so explicitly.",
            "",
            "End with a one-line **VERDICT: SHIP** (no blockers) or "
            "**VERDICT: FIX-FIRST** (one or more blockers), followed by the "
            "blocker count.",
        ]
    )
    rule = effort_rule(target.effort)
    if rule:
        lines.extend(["", rule])
    if lessons:
        lines.extend(["", lessons])
    if target.fix:
        lines.extend(
            [
                "",
                "## Then fix",
                "",
                "After the review, FIX every blocker- and warning-severity "
                "finding directly in the code, then re-run the same five-lens "
                "review on the updated diff. Repeat until the VERDICT is SHIP "
                "or no further blocker/warning findings remain. Summarize what "
                "you changed.",
            ]
        )
    return "\n".join(lines)


async def run_self_review(app: Any, raw_arg: str) -> None:  # noqa: ANN401 - the App
    """Body of `/self-review`: memo-aware (ROADMAP #67), then hand the prompt to the agent.

    `--since-last` skips when the exact diff was already reviewed at this
    effort; every run records a memo under `.bog-agents/self-review/` and the
    prompt carries the rulings from `/finding` so findings marked incorrect or
    wontfix are not repeated.
    """
    import asyncio
    from pathlib import Path

    from bog_agents_cli.self_review_memo import (
        SelfReviewMemo,
        current_branch,
        diff_fingerprint,
        lessons_block,
        load_dispositions,
        load_memo,
        marker_comment,
        review_diff_text,
        save_memo,
        should_skip,
    )
    from bog_agents_cli.widgets.messages import AppMessage

    try:
        target = parse_self_review_args(raw_arg)
    except ValueError as exc:
        await app._mount_message(
            AppMessage(
                f'{exc}\nUsage: /self-review [--staged | --branch <base> | <ref>] [--fix] [--since-last] [--effort default|high|custom:"<rule>"]'
            )
        )
        return
    repo = Path(getattr(app, "_cwd", None) or Path.cwd())
    branch = await asyncio.to_thread(current_branch, repo)
    diff_text = await asyncio.to_thread(
        review_diff_text, repo, scope=target.scope, ref=target.ref
    )
    sha = diff_fingerprint(diff_text)
    memo = load_memo(repo, branch)
    if target.since_last and should_skip(memo, diff_sha=sha, effort=target.effort):
        when = memo.reviewed_at if memo else 0.0
        await app._mount_message(
            AppMessage(
                f"Skipped: this exact diff ({sha[:12]}) was already reviewed on `{branch}` at effort "
                f"{memo.effort if memo else target.effort} (memo {when:.0f}). Run without --since-last to force."
            )
        )
        return
    lessons = lessons_block(load_dispositions(repo))
    prompt = generate_self_review_prompt(target, lessons=lessons)
    save_memo(
        repo,
        SelfReviewMemo(
            branch=branch,
            scope=target.scope,
            base=target.ref,
            diff_sha=sha,
            effort=target.effort,
        ),
    )
    announce = (
        "Running self-review gate (5 lenses) and fixing blockers..."
        if target.fix
        else "Running self-review gate (5 lenses)..."
    )
    extra = f" effort={target.effort}" if target.effort != "default" else ""
    await app._mount_message(AppMessage(f"{announce}{extra}  {marker_comment(sha)}"))
    await app._send_prompt_to_agent(prompt)


async def run_resolve(app: Any, raw_arg: str) -> None:  # noqa: ANN401 - the App
    """Body of `/finding <finding-id> addressed|wontfix|incorrect [note]` (ROADMAP #67)."""
    from pathlib import Path

    from bog_agents_cli.self_review_memo import (
        DISPOSITIONS,
        current_branch,
        load_dispositions,
        record_disposition,
    )
    from bog_agents_cli.widgets.messages import AppMessage

    words = raw_arg.split()
    if len(words) < 2 or words[1].lower() not in DISPOSITIONS:
        recent = load_dispositions(Path(getattr(app, "_cwd", None) or Path.cwd()))[-5:]
        listing = (
            "\n".join(
                f"- {d.finding_id}: {d.disposition}{' — ' + d.note if d.note else ''}"
                for d in recent
            )
            or "(no rulings yet)"
        )
        await app._mount_message(
            AppMessage(
                f"Usage: /finding <finding-id> addressed|wontfix|incorrect [note]\nRecent rulings:\n{listing}"
            )
        )
        return
    repo = Path(getattr(app, "_cwd", None) or Path.cwd())
    record = record_disposition(
        repo,
        words[0],
        words[1].lower(),
        note=" ".join(words[2:]),
        branch=current_branch(repo),
    )
    hint = (
        " — the next review prompt will carry this ruling"
        if record.disposition in ("incorrect", "wontfix")
        else ""
    )
    await app._mount_message(
        AppMessage(f"Recorded {record.finding_id} as {record.disposition}{hint}.")
    )
