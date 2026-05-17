"""Expert Mode setup wizard — guided interactive rule authoring.

Goal: turn the four-step "I know I want a policy, I'm not sure how to
write it in YAML" friction into a one-shot guided flow. The wizard
defines a small catalog of common policy categories (safety, budget,
prod-env gates, testing, generic custom), each with:

* A short user-facing description.
* A specialised system-prompt fragment that frames the LLM's authoring
  step for that category.
* A list of *example intents* the user can use as a starting point.

When the user runs ``/expert wizard <category> [free-form intent]``,
the wizard combines the category's fragment with the user's intent
and routes through the existing :func:`build_proposal` pipeline so
lint + replay + save-suggestion all still work. ``/expert wizard`` with
no args prints the menu.

Pure-logic module — model is injected for offline tests. CLI wiring
lives in :mod:`bog_agents_cli.expert_controller`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bog_agents.middleware.expert_engine.authoring import (
    AuthoringProposal,
    build_proposal,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WizardCategory:
    """One entry in the wizard's catalog.

    Attributes:
        key: Short kebab-case identifier for slash invocations
            (``/expert wizard <key>``).
        title: Human-readable name shown in the menu.
        description: One-sentence summary the user sees.
        questions: A handful of clarifying questions the wizard
            preprends to the LLM prompt. The LLM is instructed to use
            them as a checklist, not literally; the user can opt to
            answer them inline in the intent.
        intent_examples: Sample intents that show the kind of thing the
            user can type to get a good rule out of this category.
        framing: System-prompt fragment that augments the default
            authoring prompt for this category. Tells the model what
            kind of rule to bias toward (deny / require_approval /
            modify / audit) without overriding the user's actual ask.
    """

    key: str
    title: str
    description: str
    questions: tuple[str, ...]
    intent_examples: tuple[str, ...]
    framing: str


_DEFAULT_CATALOG: tuple[WizardCategory, ...] = (
    WizardCategory(
        key="safety",
        title="Safety / destructive commands",
        description=(
            "Block or require approval for commands that can cause "
            "irreversible damage (rm -rf, format, drop table, force-push)."
        ),
        questions=(
            "Which command shape(s) should be blocked or gated?",
            "Block entirely or require human approval first?",
            "Should the rule apply everywhere or only in specific "
            "environments (e.g. prod)?",
        ),
        intent_examples=(
            "Block any `rm -rf` that targets the home directory",
            "Require approval before `git push --force` to main/master",
            "Block `drop table` SQL statements in shell tool calls",
        ),
        framing=(
            "The user is describing a safety / destructive-command policy. "
            "Prefer ``deny`` for clear policy violations, "
            "``require_approval`` for higher-risk cases the user might want "
            "to override case-by-case. Always pair with ``audit_log`` so "
            "blocked attempts are recoverable evidence."
        ),
    ),
    WizardCategory(
        key="budget",
        title="Cost / budget caps",
        description=(
            "Warn or gate when the running session spend (cost_tracker) "
            "crosses configurable thresholds."
        ),
        questions=(
            "At what session spend (USD) should a warning fire?",
            "At what spend should new tool calls require approval (or stop)?",
            "Should the rule fire once-per-session or on every breach?",
        ),
        intent_examples=(
            "Warn me when session spend crosses $2 and stop at $10",
            "Require approval after $5 spent in any single session",
            "Notify slack when cumulative cost hits $20",
        ),
        framing=(
            "The user is describing a cost/budget policy. The engine "
            "asserts a ``session`` fact with a ``cost_usd`` field. "
            "Use ``session.cost_usd: { gt: N }`` patterns. Prefer "
            "``notify`` for warnings, ``require_approval`` for hard "
            "ceilings, and set ``once: true`` so the warning doesn't "
            "spam every subsequent call."
        ),
    ),
    WizardCategory(
        key="prod",
        title="Prod-env gates",
        description=(
            "Restrict what the agent can do when working in a "
            "production-like environment (env=prod context, sensitive "
            "repos, etc.)."
        ),
        questions=(
            "What signal identifies prod? (context.env value, repo name, "
            "branch, …)",
            "Which actions should be restricted on prod?",
            "Block entirely or require approval?",
        ),
        intent_examples=(
            "Require approval for shell commands when context.env is prod",
            "Block `kubectl apply` against the prod cluster context",
            "Require approval for any write to the main branch in /etc",
        ),
        framing=(
            "The user is describing an environment-specific gate. "
            "Bind the prod-indicating fact (``context``, ``session``, "
            "or a custom fact) with ``$bind`` so the deny/approval "
            "message can reference the actual environment value. "
            "Default to ``require_approval`` unless the user is "
            "explicit about wanting a hard deny."
        ),
    ),
    WizardCategory(
        key="testing",
        title="Testing / CI policy",
        description=(
            "Enforce test/lint/CI conventions — e.g. tests must run "
            "before push, lint failures must be approved, etc."
        ),
        questions=(
            "Which tool calls trigger the rule (git push, deploy, etc.)?",
            "What evidence indicates tests/lint passed (a tool_call "
            "name, a context fact)?",
            "Block or warn?",
        ),
        intent_examples=(
            "Require approval before `git push` if the last `pytest` "
            "tool call had a non-zero exit",
            "Warn when shell_execute runs a deploy script without a "
            "preceding test run",
        ),
        framing=(
            "The user is describing a testing/CI workflow rule. "
            "Patterns often combine two facts: one for the "
            "potentially-risky action (deploy, push), and one for the "
            "evidence (test result, CI status). Use ``$not: true`` for "
            "the evidence check when the absence of a green signal is "
            "what should gate the action."
        ),
    ),
    WizardCategory(
        key="custom",
        title="Custom policy (free-form)",
        description=(
            "Anything else — describe what you want in plain English and "
            "the LLM will draft the rule with no category bias."
        ),
        questions=(
            "What event or condition should trigger the rule?",
            "What should happen when it triggers?",
            "Should it always fire or only once per session?",
        ),
        intent_examples=(
            "Audit-log every tool call that touches the .ssh directory",
            "Route any task containing 'database migration' to a "
            "subagent named db-specialist",
        ),
        framing=(
            "No category bias — let the user's intent drive every "
            "decision (action verb, salience, once flag)."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Public catalog accessor
# ---------------------------------------------------------------------------


def default_catalog() -> tuple[WizardCategory, ...]:
    """Return the built-in wizard category catalog."""
    return _DEFAULT_CATALOG


def find_category(
    key: str,
    *,
    catalog: Sequence[WizardCategory] = _DEFAULT_CATALOG,
) -> WizardCategory | None:
    """Look up a category by case-insensitive key."""
    needle = (key or "").strip().lower()
    return next((c for c in catalog if c.key == needle), None)


# ---------------------------------------------------------------------------
# Wizard run
# ---------------------------------------------------------------------------


@dataclass
class WizardRun:
    """Outcome of one wizard invocation.

    Attributes:
        category: The chosen :class:`WizardCategory`, or ``None`` for a
            menu-only invocation.
        proposal: The :class:`AuthoringProposal` produced (or ``None``
            when the wizard was menu-only / errored).
        error: Free-form error message for the user.
    """

    category: WizardCategory | None = None
    proposal: AuthoringProposal | None = None
    error: str = ""
    intent_used: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def menu_text(catalog: Sequence[WizardCategory] = _DEFAULT_CATALOG) -> str:
    """Render the wizard's category menu for ``/expert wizard`` (no args)."""
    lines = [
        "Expert Mode setup wizard — pick a category and describe what you want:",
        "",
    ]
    for cat in catalog:
        lines.append(f"  {cat.key:<8} — {cat.title}")
        lines.append(f"           {cat.description}")
        lines.append("")
    lines.append(
        "Usage: /expert wizard <category> [your intent in plain English]"
    )
    lines.append("Example: /expert wizard safety block rm -rf on prod hosts")
    lines.append("")
    lines.append(
        "Sample intents you can copy:"
    )
    for cat in catalog:
        for example in cat.intent_examples[:1]:
            lines.append(f"  /expert wizard {cat.key} {example}")
    return "\n".join(lines)


def run_wizard(
    *,
    category_key: str,
    intent: str,
    model: BaseChatModel,
    history: Sequence[dict[str, Any]] = (),
    catalog: Sequence[WizardCategory] = _DEFAULT_CATALOG,
) -> WizardRun:
    """Drive one wizard step — picking a category and authoring a proposal.

    Args:
        category_key: Wizard category id (see :data:`_DEFAULT_CATALOG`).
        intent: Free-form intent. When empty, returns a category-
            specific help message instead of calling the LLM.
        model: Chat model used to draft the YAML.
        history: Recent tool-call records for replay.
        catalog: Override the built-in catalog (mostly for tests).

    Returns:
        :class:`WizardRun`. Use ``run.proposal`` for the rendered
        output and the user-approval flow.
    """
    category = find_category(category_key, catalog=catalog)
    if category is None:
        return WizardRun(
            error=(
                f"Unknown wizard category: {category_key!r}. "
                f"Choose from: {', '.join(c.key for c in catalog)}"
            )
        )
    if not intent.strip():
        return WizardRun(
            category=category,
            error=(
                f"{category.title}: please describe what you want.\n"
                "Helpful questions:\n"
                + "\n".join(f"  - {q}" for q in category.questions)
                + "\n\nExamples:\n"
                + "\n".join(f"  /expert wizard {category.key} {x}" for x in category.intent_examples)
            ),
        )
    framed_intent = _frame_intent(category, intent)
    proposal = build_proposal(framed_intent, model=model, history=list(history))
    return WizardRun(
        category=category,
        proposal=proposal,
        intent_used=framed_intent,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _frame_intent(category: WizardCategory, intent: str) -> str:
    """Prepend category framing + checklist questions to the user's intent.

    The downstream :func:`build_proposal` will pass this whole block to
    the model's system prompt as the user's "Write the YAML for the
    rule that implements …" prompt. The category framing biases the
    model toward the right action verbs without overriding the user's
    actual policy ask.
    """
    chunks = [
        f"Category: {category.title}",
        f"Framing: {category.framing}",
        "",
        "Checklist for this category (treat as guidance, not literal questions):",
    ]
    chunks.extend(f"  - {q}" for q in category.questions)
    chunks.append("")
    chunks.append(f"User's intent: {intent.strip()}")
    return "\n".join(chunks)


__all__ = [
    "WizardCategory",
    "WizardRun",
    "default_catalog",
    "find_category",
    "menu_text",
    "run_wizard",
]
