"""LLM-driven rule authoring for Expert Mode (REVIEW.md T-11 v2 #4).

Workflow:

1. User describes a policy in natural language (``/expert write``).
2. LLM generates a YAML snippet conforming to the expert-rule grammar.
3. We validate the YAML via :mod:`loader` and lint it via :mod:`lint`.
4. Optionally dry-run the proposed rules against a list of *historical*
   tool_call snapshots so the user sees what they would have done.
5. The user reviews + approves; on approve we append the YAML to a
   target file in ``.bog-agents/expert_rules/``.

This module is the pure-logic layer — accepts the model as an argument
so tests run offline. The CLI wiring (slash command, prompting, file
write confirmation) lives in :mod:`bog_agents_cli.expert_controller`.

The authoring loop deliberately keeps every step inspectable: the user
sees the prompt the LLM received, the YAML it produced, the lint
report, AND the dry-run outcomes before any disk write happens. This
is the bridge between LLM ergonomics and rule-based determinism — the
one missing piece nobody else has wired up in 2026.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bog_agents.middleware.expert_engine.engine import ExpertEngine
from bog_agents.middleware.expert_engine.lint import lint, render_report
from bog_agents.middleware.expert_engine.loader import (
    RuleLoadError,
    load_rules_from_string,
)
from bog_agents.middleware.expert_engine.types import Fact
from bog_agents.middleware.expert_engine.working_memory import WorkingMemory

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from bog_agents.middleware.expert_engine.lint import LintReport
    from bog_agents.middleware.expert_engine.types import Rule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


_RULE_AUTHOR_PROMPT = """You are an expert at writing YAML rules for the
bog-agents Expert Mode rule engine.

The engine runs *before* every tool call. It asserts a ``tool_call``
fact with these fields:

* ``name`` — tool name (e.g. ``shell_execute``)
* ``args`` — tool arguments dict
* ``command`` — flattened convenience field equal to ``args.command``
  when present (the shell tool's command line)
* ``id`` — opaque tool-call id from the model

Rules can also pattern-match these fact types when they are present
in working memory: ``session`` (with ``cost_usd``, ``id``, ``env``),
``context`` (free-form metadata about the working environment),
``file_edit``, and any custom fact_type produced by another rule's
``assert_fact`` action.

A rule has this YAML shape::

    - name: prod_force_push_gate            # unique kebab/snake case identifier
      description: One-line human summary    # optional
      salience: 100                          # higher fires first (default 0)
      once: false                            # fire at most once per run
      when:
        - tool_call:
            name: shell_execute
            command:
              matches: 'git push.*--force.*main'
      then:
        - deny: "Force-push to main is prohibited."
        - audit_log:
            event: prod_force_push_blocked

Predicate operators inside a ``field: { op: value }`` map:

* ``eq``, ``ne``       — equality / inequality
* ``in``, ``not_in``   — membership (list)
* ``gt``, ``gte``, ``lt``, ``lte`` — numeric comparison
* ``matches``           — Python regex search on a string
* ``contains``          — substring / list-contains
* ``exists``, ``missing`` — field presence test

A scalar value (``name: shell``) is shorthand for ``eq``. Two special
keys inside a pattern: ``$bind: var`` binds the matched fact, and
``$not: true`` negates the pattern (negation as failure).

Action verbs in ``then:``:

* ``deny``               — block the tool call. ``reason: "..."``
* ``modify``             — overwrite tool args. ``timeout: 30`` etc.
* ``require_approval``   — pause for human approval. ``gate``, ``risk``, ``reason``
* ``notify``             — side-channel notify. ``channel``, ``severity``, ``text``
* ``audit_log``          — write event to audit trail. ``event``, ``...``
* ``assert_fact``        — add new fact to working memory.
                          ``fact_type``, ``data``
* ``retract_fact``       — remove a fact. ``fact_id`` or ``fact_type``
* ``route_to_subagent``  — hand off. ``agent``
* ``ask_llm``            — escape hatch. ``prompt``

User intent will be in plain English. Your job:

1. Translate it into the *smallest* rule that captures the intent.
2. Prefer specific over general. Match the narrowest set of tool calls
   that could plausibly trigger the user's concern.
3. Choose actions that the user can clearly map back to what they said.
   "Block" → ``deny``. "Ask me first" → ``require_approval``.
   "Tell me" → ``notify``. "Log it" → ``audit_log``.
4. Pick salience values that make sense relative to the intent's
   priority. Hard policy violations: 100. Soft warnings: 50.
5. When uncertain whether the user wants a hard or soft enforcement,
   pick ``require_approval`` — it's the safe middle.
6. Output ONLY the YAML for the rule(s), no commentary, no fences
   beyond the YAML itself. Multiple rules are fine if the intent
   naturally splits.

DO NOT write rules that would deny ALL tool calls — that bricks the
agent. DO NOT use ``ask_llm`` for the primary action unless the user
explicitly asks for human-vs-LLM ambiguity handling.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ReplayOutcome:
    """Per-historical-call outcome of a dry-run during authoring.

    Attributes:
        snapshot: The ``tool_call`` fact data used for the replay.
        denied: True if any of the proposed rules would have denied this
            call.
        deny_reasons: Deny strings from the proposed rules.
        modifications: Merged modify-action params from the proposed rules.
        approvals: Approval gates the proposed rules would have raised.
        fired_rules: Names of rules that fired against this snapshot.
    """

    snapshot: dict[str, Any]
    denied: bool = False
    deny_reasons: list[str] = field(default_factory=list)
    modifications: dict[str, Any] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    fired_rules: list[str] = field(default_factory=list)


@dataclass
class AuthoringProposal:
    """The full proposal the user sees before deciding to save.

    Attributes:
        intent: The user's natural-language ask.
        yaml: The model-generated YAML text.
        rules: Parsed :class:`Rule` objects (empty list if parse failed).
        parse_error: Empty when ``rules`` is populated.
        lint: Static-analysis report over the parsed rules.
        replay: Per-snapshot outcomes from the historical dry-run.
        replay_count_denied: Convenience count of replay denials.
        replay_count_modified: Convenience count of replays modified.
        suggested_filename: Default filename for save (kebab-cased
            from the first rule name).
    """

    intent: str
    yaml: str = ""
    rules: list[Rule] = field(default_factory=list)
    parse_error: str = ""
    lint: LintReport | None = None
    replay: list[ReplayOutcome] = field(default_factory=list)
    replay_count_denied: int = 0
    replay_count_modified: int = 0
    suggested_filename: str = ""

    @property
    def ok_to_save(self) -> bool:
        """True iff the proposal parsed and has no lint errors."""
        if not self.rules or self.parse_error:
            return False
        if self.lint is None:
            return True
        return not self.lint.errors


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


def generate_yaml(
    intent: str,
    *,
    model: BaseChatModel,
    system_prompt: str = _RULE_AUTHOR_PROMPT,
) -> str:
    r"""Ask the model to generate YAML rule(s) implementing *intent*.

    Strips common decorations (``\`\`\`yaml`` fences, leading commentary)
    so the result is feed-ready to :func:`load_rules_from_string`.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    if not intent.strip():
        return ""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=(f"Write the YAML for the rule(s) that implement this policy:\n\n{intent.strip()}")),
    ]
    response = model.invoke(messages)
    raw = getattr(response, "content", response)
    if isinstance(raw, list):
        # Multi-block content list → join text parts.
        text = "".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
    else:
        text = str(raw)
    return _strip_yaml_fences(text)


def _strip_yaml_fences(text: str) -> str:
    r"""Remove ``\`\`\`yaml`` / ``\`\`\``` markdown fences from *text*."""
    lines = text.splitlines()
    # Trim leading blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    # Open fence.
    if lines and lines[0].strip().startswith("```"):
        lines.pop(0)
    # Trailing fence.
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Build a proposal
# ---------------------------------------------------------------------------


def build_proposal(
    intent: str,
    *,
    model: BaseChatModel,
    history: Sequence[dict[str, Any]] = (),
) -> AuthoringProposal:
    """Generate YAML, parse it, lint it, and dry-run against *history*.

    Args:
        intent: The user's natural-language description of the policy.
        model: Chat model used to draft the YAML.
        history: List of historical ``tool_call`` data dicts to replay
            the proposed rules against. Empty disables replay.

    Returns:
        :class:`AuthoringProposal`. Always returns — never raises.
        Check ``ok_to_save`` before persisting.
    """
    proposal = AuthoringProposal(intent=intent.strip())
    try:
        proposal.yaml = generate_yaml(intent, model=model)
    except Exception as exc:
        proposal.parse_error = f"model call failed: {exc}"
        return proposal

    if not proposal.yaml.strip():
        proposal.parse_error = "model returned no YAML"
        return proposal

    try:
        proposal.rules = load_rules_from_string(proposal.yaml)
    except RuleLoadError as exc:
        proposal.parse_error = str(exc)
        return proposal

    proposal.lint = lint(proposal.rules)
    proposal.suggested_filename = _suggest_filename(proposal.rules)
    proposal.replay = _replay_against_history(proposal.rules, history)
    proposal.replay_count_denied = sum(1 for r in proposal.replay if r.denied)
    proposal.replay_count_modified = sum(1 for r in proposal.replay if r.modifications)
    return proposal


def _suggest_filename(rules: list[Rule]) -> str:
    """Pick a sensible YAML filename from the first rule's name."""
    if not rules:
        return "expert-rule.yaml"
    base = rules[0].name.strip().lower()
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base)
    safe = "-".join(filter(None, safe.split("-")))
    return f"{safe or 'expert-rule'}.yaml"


# ---------------------------------------------------------------------------
# Historical replay
# ---------------------------------------------------------------------------


def _replay_against_history(
    rules: list[Rule],
    history: Sequence[dict[str, Any]],
) -> list[ReplayOutcome]:
    """Replay *rules* against each historical tool_call dict.

    Each entry in *history* is treated as the ``data`` payload for a
    ``tool_call`` fact. The engine runs to a fixed point against a
    fresh working memory per snapshot (so cross-snapshot state doesn't
    leak), then we record what would have happened.

    Returns a per-snapshot :class:`ReplayOutcome` list.
    """
    out: list[ReplayOutcome] = []
    for snapshot in history:
        if not isinstance(snapshot, dict):
            continue
        memory = WorkingMemory()
        engine = ExpertEngine(rules, memory=memory)
        memory.assert_fact(Fact(fact_type="tool_call", data=dict(snapshot)))
        result = engine.run()
        outcome = ReplayOutcome(snapshot=dict(snapshot))
        outcome.denied = result.denied
        outcome.deny_reasons = list(result.deny_reasons)
        outcome.modifications = result.actions.merged_modification()
        outcome.approvals = list(result.actions.approvals_required)
        outcome.fired_rules = [a.rule.name for a in result.activations]
        out.append(outcome)
    return out


# ---------------------------------------------------------------------------
# Rendering for the CLI
# ---------------------------------------------------------------------------


def render_proposal(proposal: AuthoringProposal) -> str:
    """Render *proposal* as plain text for the ``/expert write`` flow."""
    lines: list[str] = []
    lines.append("== Expert rule proposal ==")
    lines.append(f"Intent: {proposal.intent}")
    lines.append("")
    if proposal.parse_error:
        lines.append(f"Parse error: {proposal.parse_error}")
        if proposal.yaml:
            lines.append("")
            lines.append("Model output (could not parse):")
            lines.append("---")
            lines.append(proposal.yaml.rstrip())
            lines.append("---")
        return "\n".join(lines)

    lines.append(f"Generated {len(proposal.rules)} rule(s); suggested file: {proposal.suggested_filename}")
    lines.append("")
    lines.append("YAML:")
    lines.append("---")
    lines.append(proposal.yaml.rstrip())
    lines.append("---")
    lines.append("")
    if proposal.lint is not None:
        lines.append(render_report(proposal.lint))
        lines.append("")
    if proposal.replay:
        lines.append(
            f"Replay against {len(proposal.replay)} historical tool_call(s): "
            f"{proposal.replay_count_denied} would have been denied, "
            f"{proposal.replay_count_modified} modified."
        )
        for i, r in enumerate(proposal.replay, start=1):
            mark = "deny" if r.denied else ("modify" if r.modifications else ("approve" if r.approvals else "pass"))
            ident = r.snapshot.get("name", "?")
            lines.append(f"  [{i:>3}] {mark:<8} tool={ident!r} fired={r.fired_rules}")
    else:
        lines.append("Replay: no history provided.")
    lines.append("")
    if proposal.ok_to_save:
        lines.append(f"Approve with: /expert write save {proposal.suggested_filename}")
    else:
        lines.append("Cannot save: fix the errors above (rerun /expert write to retry).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def save_proposal(
    proposal: AuthoringProposal,
    *,
    rules_dir: Any,
    filename: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Write *proposal*'s YAML to ``<rules_dir>/<filename>`` and return the path.

    Args:
        proposal: Approved proposal. Must satisfy ``proposal.ok_to_save``.
        rules_dir: Target directory (typically
            ``<cwd>/.bog-agents/expert_rules``). Created if missing.
        filename: Override the suggested name. Must end in ``.yaml`` /
            ``.yml`` and contain no path separators.
        overwrite: When False (default), refuse to clobber an existing
            file. When True, silently replaces.

    Raises:
        ValueError: When the proposal isn't OK to save, the filename is
            unsafe, or the target exists and ``overwrite=False``.

    Returns:
        Absolute :class:`Path` of the written file.
    """
    from pathlib import Path

    if not proposal.ok_to_save:
        msg = "proposal is not ok_to_save (parse error or lint errors)"
        raise ValueError(msg)
    name = filename or proposal.suggested_filename
    if "/" in name or "\\" in name:
        msg = f"filename {name!r} must not contain path separators"
        raise ValueError(msg)
    if not name.endswith((".yaml", ".yml")):
        msg = f"filename {name!r} must end in .yaml or .yml"
        raise ValueError(msg)
    target_dir = Path(rules_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    if target.exists() and not overwrite:
        msg = f"{target} already exists — pass overwrite=True to replace"
        raise ValueError(msg)
    target.write_text(proposal.yaml, encoding="utf-8")
    return target


__all__ = [
    "AuthoringProposal",
    "ReplayOutcome",
    "build_proposal",
    "generate_yaml",
    "render_proposal",
    "save_proposal",
]
