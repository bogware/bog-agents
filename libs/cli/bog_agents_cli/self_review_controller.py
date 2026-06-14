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

from dataclasses import dataclass

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
    tokens = args.split()
    fix = False
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--fix":
            fix = True
        elif tok == "--staged":
            rest.append("__staged__")
        elif tok == "--branch" and i + 1 < len(tokens):
            return SelfReviewTarget(scope="branch", ref=tokens[i + 1], fix=fix)
        else:
            rest.append(tok)
        i += 1

    if "__staged__" in rest:
        return SelfReviewTarget(scope="staged", fix=fix)

    concrete = [t for t in rest if t != "__staged__"]
    if concrete:
        # A bare ref like HEAD~1 / a sha -> single-commit review.
        return SelfReviewTarget(scope="commit", ref=concrete[0], fix=fix)

    return SelfReviewTarget(scope="working", fix=fix)


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


def generate_self_review_prompt(target: SelfReviewTarget) -> str:
    """Build the multi-lens self-review prompt for the agent.

    Args:
        target: What to review and whether to fix.

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
