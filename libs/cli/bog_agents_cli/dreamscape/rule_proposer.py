"""Dreamscape → Expert Mode rule proposer (REVIEW.md D-1 + T-11 v2 #5).

The killer pairing: dreamscape already records every session as a
phase-snapshot markdown file under
``~/.bog-agents/dreamscape/<agent>/dreams/``. This module mines those
artifacts for repeated patterns (denials, failed tool calls, costly
commands), asks the LLM to draft rules that would codify the lesson,
and stashes the proposals as YAML under
``<project>/.bog-agents/expert_rules/proposals/`` — a separate
directory so the proposals **do not auto-activate**. The user reviews
via ``/expert proposals`` and promotes via ``/expert proposals approve``.

This is the bridge between long-term memory and deterministic policy:
the agent doesn't just remember what happened, it suggests rules to
make sure the next session benefits.

Pure-logic module — the model is injected, so tests run offline. The
CLI wiring lives in :mod:`bog_agents_cli.expert_controller`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bog_agents.middleware.expert_engine import (
    AuthoringProposal,
    build_proposal,
)

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Proposer system prompt
# ---------------------------------------------------------------------------


_PROPOSER_SYSTEM_PROMPT = """You are a careful rule designer who reviews
an agent's recent activity and proposes deterministic policy rules
that would have caught the worst mistakes or codified the most
useful patterns.

You receive two kinds of evidence:

* **Dream snapshots** — markdown summaries of recent sessions. They
  describe what the agent did, where it succeeded or struggled,
  recurring themes, and any noted concerns.
* **Tool-call history** — the most recent JSON-ish records of tool
  invocations the agent made.

Your job:

1. Identify **at most 3** patterns that recur and are worth codifying
   as a deterministic rule. Skip one-off oddities.
2. For each pattern, draft a YAML expert-rule (the schema you've
   been trained on in /expert write). Prefer ``require_approval`` for
   anything uncertain; reserve ``deny`` for clear policy violations.
3. Write a short ``description`` field that names the source dream
   (or summary of the recurring observation) so the user can trace
   the reasoning back.
4. Emit ONLY YAML — one or more rules in a single document. No
   commentary, no markdown fences.

DO NOT propose rules that block normal development workflows (e.g.
"deny every shell_execute"). DO NOT propose rules that already exist
in the agent's loaded rulebook (the user will tell you what's
already in place).

If the evidence is too thin to propose anything responsible, output
the literal string ``# no-proposals`` instead of YAML — the caller
will treat that as a clean no-op.
"""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ProposalRun:
    """Outcome of one proposer invocation.

    Attributes:
        agent_id: Which dreamscape agent we mined.
        dream_sources: Paths of the dream files we read.
        proposal: The :class:`AuthoringProposal` the LLM produced (or
            ``None`` when the LLM emitted ``# no-proposals``).
        saved_path: Path the proposal YAML was written to, or ``None``
            if the run errored / produced no proposals / was a dry-run.
        skipped: True iff the LLM declined to propose anything.
        error: Free-form error string when the run failed.
    """

    agent_id: str
    dream_sources: list[Path] = field(default_factory=list)
    proposal: AuthoringProposal | None = None
    saved_path: Path | None = None
    skipped: bool = False
    error: str = ""
    # True when the proposal was written to the *active* rules dir
    # (auto_activate path) rather than the staging proposals dir.
    # The caller should trigger a middleware reload when this is True
    # so the rule takes effect immediately. Distinct from
    # ``saved_path is not None`` because the staging path also sets
    # ``saved_path`` — ``active`` is the marker for "live without
    # further user approval needed".
    active: bool = False


# ---------------------------------------------------------------------------
# Dream collection
# ---------------------------------------------------------------------------


def _read_recent_dreams(
    agent_id: str,
    *,
    limit: int,
    max_chars_per_dream: int,
) -> list[tuple[Path, str]]:
    """Return ``(path, text)`` for the *limit* most-recent dreams.

    The text is truncated to *max_chars_per_dream* so a single
    long-running session can't dominate the model's prompt budget.
    """
    # Import lazily — keeps this module callable from tests that don't
    # have the dreamscape state directory wired up.
    from bog_agents_cli.dreamscape.dream_engine import list_agent_dreams

    out: list[tuple[Path, str]] = []
    for path in list_agent_dreams(agent_id, limit=limit):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read dream %s: %s", path, exc)
            continue
        if len(text) > max_chars_per_dream:
            text = text[:max_chars_per_dream] + "\n\n…[truncated]…"
        out.append((path, text))
    return out


def _format_tool_history(history: Sequence[dict[str, Any]], *, limit: int = 40) -> str:
    """Render a recent slice of the engine's ``tool_call_history`` as text."""
    if not history:
        return "(no recent tool calls recorded)"
    tail = list(history)[-limit:]
    lines = []
    for i, call in enumerate(tail, start=1):
        name = call.get("name", "?")
        cmd = call.get("command") or call.get("args", {})
        lines.append(f"  [{i:>3}] {name}: {cmd!s:.180}")
    return "\n".join(lines)


def _format_existing_rules(rule_names: Sequence[str]) -> str:
    """Compact summary of rules the user already has in place."""
    if not rule_names:
        return "(no rules currently loaded)"
    return ", ".join(rule_names)


# ---------------------------------------------------------------------------
# Build the prompt the LLM sees
# ---------------------------------------------------------------------------


def build_intent(
    *,
    agent_id: str,
    dreams: Sequence[tuple[Path, str]],
    tool_history: Sequence[dict[str, Any]],
    existing_rules: Sequence[str],
) -> str:
    """Compose the ``intent`` string the authoring pipeline sees.

    We pre-format the evidence rather than relying on the proposer's
    system prompt to do the framing, because the downstream
    :func:`build_proposal` call already has its own system prompt that
    explains the rule grammar — this intent is the *evidence* payload.
    """
    sections: list[str] = []
    sections.append(
        f"Agent id: {agent_id}\n\nReview the following recent activity from this "
        "agent. Propose at most 3 expert-mode rules that would codify the "
        "lessons or prevent the worst mistakes. Emit YAML only, or the "
        "literal text ``# no-proposals`` if the evidence is too thin."
    )
    sections.append(
        f"Existing rules already in place:\n  {_format_existing_rules(existing_rules)}"
    )
    sections.append(
        f"Recent tool-call history (most recent last):\n{_format_tool_history(tool_history)}"
    )
    if dreams:
        sections.append("Recent dreams (oldest first):")
        for path, text in reversed(dreams):
            sections.append(f"\n--- dream: {path.name} ---\n{text}")
    else:
        sections.append("Recent dreams: (none on disk)")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Top-level proposer
# ---------------------------------------------------------------------------


def propose_rules(
    *,
    agent_id: str,
    model: BaseChatModel,
    tool_history: Sequence[dict[str, Any]] = (),
    existing_rules: Sequence[str] = (),
    proposals_dir: Path | None = None,
    rules_dir: Path | None = None,
    dream_limit: int = 5,
    max_chars_per_dream: int = 4000,
    save: bool = True,
    auto_activate: bool = False,
) -> ProposalRun:
    """Generate a proposal from recent activity and (optionally) save it.

    Args:
        agent_id: Dreamscape agent id whose dreams we mine.
        model: Chat model used to draft the YAML.
        tool_history: Tool-call records from
            :attr:`ExpertRulesMiddleware.tool_call_history`. Empty is OK.
        existing_rules: Names of rules the user already has loaded,
            so the proposer doesn't duplicate them.
        proposals_dir: Where to write the YAML when ``save=True`` and
            ``auto_activate=False``. Defaults to
            ``<cwd>/.bog-agents/expert_rules/proposals``.
        rules_dir: Where to write the YAML when ``auto_activate=True``.
            Defaults to ``<cwd>/.bog-agents/expert_rules``.
        dream_limit: How many of the most-recent dreams to feed in.
        max_chars_per_dream: Per-dream truncation budget.
        save: When True (default), write the proposal YAML to disk.
            When False, returns the proposal in memory only (dry-run).
        auto_activate: When True, write the YAML directly to the
            *active* rules directory instead of the staging
            ``proposals/`` subdir. Use ONLY when you trust the proposer
            and want the rule to take effect immediately on the next
            engine reload. Default False — the staging dir is the
            REVIEW-md-mandated safety pattern. See ``ProposalRun.active``.

    Returns:
        A :class:`ProposalRun` describing what happened. When
        ``auto_activate`` is True and the save succeeded, ``active``
        is True so the caller can trigger a middleware reload.
    """
    run = ProposalRun(agent_id=agent_id)
    try:
        dreams = _read_recent_dreams(
            agent_id,
            limit=dream_limit,
            max_chars_per_dream=max_chars_per_dream,
        )
    except Exception as exc:
        run.error = f"could not read dreams for agent {agent_id!r}: {exc}"
        return run
    run.dream_sources = [p for p, _ in dreams]

    if not dreams and not tool_history:
        run.skipped = True
        run.error = "no dreams or tool history to learn from"
        return run

    intent = build_intent(
        agent_id=agent_id,
        dreams=dreams,
        tool_history=tool_history,
        existing_rules=existing_rules,
    )
    proposal = build_proposal(intent, model=model, history=list(tool_history))

    # Detect the explicit no-proposals signal.
    if proposal.yaml.strip().startswith("# no-proposals"):
        run.skipped = True
        return run
    run.proposal = proposal

    if not save:
        return run
    if not proposal.ok_to_save:
        run.error = (
            "proposal parse / lint failed — see proposal.parse_error and lint"
        )
        return run

    if auto_activate:
        target_dir = rules_dir or (
            Path.cwd() / ".bog-agents" / "expert_rules"
        )
    else:
        target_dir = proposals_dir or (
            Path.cwd() / ".bog-agents" / "expert_rules" / "proposals"
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    # Auto-activated rules go in WITHOUT a timestamp prefix so
    # repeated proposals for the same intent don't multiply files —
    # the engine reload picks the latest version. Staged proposals
    # KEEP the timestamp so the user can compare iterations.
    filename = (
        proposal.suggested_filename
        if auto_activate
        else _timestamped_filename(proposal.suggested_filename)
    )
    target = target_dir / filename
    # When auto-activating, refuse to clobber an existing rule with a
    # different name silently — that would be a footgun.
    if auto_activate and target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing != proposal.yaml:
            run.error = (
                f"refusing to overwrite existing active rule {target.name!r} "
                "with different content (auto_activate). Approve via "
                "/expert proposals or remove the existing file first."
            )
            return run
    target.write_text(proposal.yaml, encoding="utf-8")
    run.saved_path = target
    run.active = auto_activate
    return run


def _timestamped_filename(suggested: str) -> str:
    """Prefix *suggested* with a UTC date so successive proposals don't clash."""
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    stem = suggested
    if stem.endswith((".yaml", ".yml")):
        ext = "." + stem.rsplit(".", 1)[1]
        stem = stem.rsplit(".", 1)[0]
    else:
        ext = ".yaml"
    return f"{stamp}-{stem}{ext}"


# ---------------------------------------------------------------------------
# Proposal management
# ---------------------------------------------------------------------------


def list_pending_proposals(proposals_dir: Path) -> list[Path]:
    """Return all proposal YAML files currently pending review."""
    if not proposals_dir.is_dir():
        return []
    return sorted(proposals_dir.glob("*.yaml")) + sorted(proposals_dir.glob("*.yml"))


def approve_proposal(
    *,
    proposals_dir: Path,
    rules_dir: Path,
    name: str,
    overwrite: bool = False,
) -> Path:
    """Move *name* from *proposals_dir* into *rules_dir*.

    Args:
        proposals_dir: Where proposals are staged.
        rules_dir: Active rules directory (the engine reload picks
            these up).
        name: Filename of the proposal to approve. Path separators are
            rejected.
        overwrite: When False, refuse to clobber an existing rule.

    Returns:
        The path of the newly active rule file.

    Raises:
        ValueError: When *name* is unsafe, missing, or would overwrite
            an existing rule without ``overwrite=True``.
    """
    if "/" in name or "\\" in name:
        msg = f"filename {name!r} must not contain path separators"
        raise ValueError(msg)
    source = proposals_dir / name
    if not source.is_file():
        msg = f"no pending proposal named {name!r} in {proposals_dir}"
        raise ValueError(msg)
    rules_dir.mkdir(parents=True, exist_ok=True)
    target = rules_dir / name
    if target.exists() and not overwrite:
        msg = f"{target} already exists — pass overwrite=True to replace"
        raise ValueError(msg)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    source.unlink()
    return target


def discard_proposal(
    *,
    proposals_dir: Path,
    name: str,
) -> Path:
    """Permanently delete a pending proposal file.

    Args:
        proposals_dir: Staging directory.
        name: Filename of the proposal to delete (no path separators).

    Returns:
        The deleted path (for the caller's confirmation log).

    Raises:
        ValueError: When *name* is unsafe or the file is missing.
    """
    if "/" in name or "\\" in name:
        msg = f"filename {name!r} must not contain path separators"
        raise ValueError(msg)
    target = proposals_dir / name
    if not target.is_file():
        msg = f"no pending proposal named {name!r} in {proposals_dir}"
        raise ValueError(msg)
    target.unlink()
    return target


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_proposals_list(proposals_dir: Path) -> str:
    """Plain-text summary for the ``/expert proposals`` slash output."""
    items = list_pending_proposals(proposals_dir)
    if not items:
        return (
            f"No pending rule proposals in {proposals_dir}. "
            "Generate one with /expert propose."
        )
    lines = [f"{len(items)} pending proposal(s) in {proposals_dir}:"]
    for p in items:
        try:
            head = p.read_text(encoding="utf-8").splitlines()[:8]
        except OSError:
            head = ["(could not read)"]
        lines.append(f"  • {p.name}")
        for line in head:
            lines.append(f"      {line}")
        lines.append("")
    lines.append(
        "Approve with: /expert proposals approve <filename>\n"
        "Discard with: /expert proposals discard <filename>"
    )
    return "\n".join(lines)


__all__ = [
    "ProposalRun",
    "approve_proposal",
    "build_intent",
    "discard_proposal",
    "list_pending_proposals",
    "propose_rules",
    "render_proposals_list",
]
