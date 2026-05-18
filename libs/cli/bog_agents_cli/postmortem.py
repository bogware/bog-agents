"""``/postmortem <trace-id|latest>`` — feed a trace back through the model.

Reads one causal session, identifies the failure point (the last
``tool_result`` with ``is_error=True`` or, failing that, the most
recent rule-fire/deny event), packages a structured prompt for the
LLM, and asks it to produce a three-part remediation proposal:

1. **Rule** — a YAML rule that would have prevented the failure.
2. **Skill** — an update or addition to a skill that would have made
   the agent handle the situation better.
3. **Config** — a config-level toggle (env var, FeatureConfig field,
   middleware setting) that's worth flipping.

The model's answer is parsed, rendered as a structured proposal, and
optionally saved to disk under ``.bog-agents/postmortems/`` as a
fenced markdown file the user can review + commit.

This is the loop competitors can't easily copy — we already record
rule fires, dreams, and tool results in the causal log (trace-mind),
so the LLM's input is a *causal graph*, not a raw transcript.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bog_agents_cli.causal.ledger import (
    CausalEvent,
    EventKind,
    list_sessions,
    load_session,
)
from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)


_POSTMORTEM_SYSTEM_PROMPT = """\
You are reviewing an agent run that produced a surprising or failing
outcome. The user has captured the run as a causal event log: each
event has a kind (user_message, model_call, tool_call, tool_result,
rule_fire, dream_complete, final_answer, note), an actor, a short
summary, and parent ids that thread the events into a causal graph.

Your job: produce a remediation proposal with three sections.

1. ## Rule
   A YAML expert rule (matching the bog-agents expert-rules schema)
   that would have prevented or modified the bad behavior. If a rule
   is not the right answer, write "(no rule needed)" and explain why.

2. ## Skill
   A short markdown skill description that, if loaded, would have
   helped the model handle the situation better. If a skill isn't
   the right answer, write "(no skill needed)".

3. ## Config
   A one-line config change (env var or FeatureConfig field), or
   "(no config change)".

Be concrete. Prefer the smallest fix that would have changed the
outcome. Output ONLY the three Markdown sections — no commentary
before or after.
"""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailurePoint:
    """The event the postmortem treats as the root failure.

    Attributes:
        event: The triggering causal event.
        reason: Short label — "tool_error", "rule_denied",
            "final_answer_unsatisfactory", "no_failure_detected".
        ancestry: The chain of ancestor events leading to it,
            youngest first. Renders as the proximate cause.
    """

    event: CausalEvent | None
    reason: str
    ancestry: tuple[CausalEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class Proposal:
    """The parsed LLM output."""

    rule_yaml: str
    """Rule YAML or "(no rule needed)" — preserve as-emitted."""

    skill_markdown: str
    config_change: str

    raw: str
    """Full unparsed model output, for audit + debugging."""


@dataclass(frozen=True, slots=True)
class PostmortemRun:
    """End-to-end result of one ``/postmortem`` call."""

    session_id: str
    failure: FailurePoint
    proposal: Proposal | None
    saved_path: Path | None = None
    error: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    enrollment: "object | None" = None  # noqa: UP037 — forward ref to EnrolledProposal
    """Wave T: optional :class:`bog_agents_cli.feedback_loop.EnrolledProposal`
    when the caller asked us to enroll the rule + skill into the
    proposer pipeline. ``None`` when ``enroll=False`` (the default)."""


# ---------------------------------------------------------------------------
# Failure-point detection
# ---------------------------------------------------------------------------


def find_failure_point(events: list[CausalEvent]) -> FailurePoint:
    """Identify the most useful "thing went wrong" event in *events*.

    Strategy:

    1. The *last* ``TOOL_RESULT`` whose payload reports ``is_error``.
    2. Failing that, the last ``RULE_FIRE`` with ``action == "deny"``
       in payload (a rule blocked something, which is often the
       "failure" the user is reviewing).
    3. Failing that, the last ``FINAL_ANSWER`` (we treat surprising
       final answers as the implicit failure point — the user opened
       the postmortem because the answer was wrong).
    4. Otherwise no failure detected.
    """
    if not events:
        return FailurePoint(event=None, reason="no_failure_detected")

    by_id = {e.id: e for e in events}

    for event in reversed(events):
        if event.kind == EventKind.TOOL_RESULT and event.payload.get("is_error"):
            return FailurePoint(
                event=event,
                reason="tool_error",
                ancestry=_walk_ancestry(event, by_id),
            )
    for event in reversed(events):
        if (
            event.kind == EventKind.RULE_FIRE
            and (event.payload.get("action") or "").lower() == "deny"
        ):
            return FailurePoint(
                event=event,
                reason="rule_denied",
                ancestry=_walk_ancestry(event, by_id),
            )
    for event in reversed(events):
        if event.kind == EventKind.FINAL_ANSWER:
            return FailurePoint(
                event=event,
                reason="final_answer_unsatisfactory",
                ancestry=_walk_ancestry(event, by_id),
            )
    return FailurePoint(event=None, reason="no_failure_detected")


def _walk_ancestry(
    root: CausalEvent, by_id: dict[int, CausalEvent], *, limit: int = 25
) -> tuple[CausalEvent, ...]:
    """Walk parent_ids back to the root user_message, youngest first."""
    out: list[CausalEvent] = []
    seen: set[int] = set()
    frontier: list[int] = list(root.parent_ids)
    while frontier and len(out) < limit:
        cur_id = frontier.pop(0)
        if cur_id in seen:
            continue
        seen.add(cur_id)
        cur = by_id.get(cur_id)
        if cur is None:
            continue
        out.append(cur)
        frontier.extend(p for p in cur.parent_ids if p not in seen)
    return tuple(out)


# ---------------------------------------------------------------------------
# Prompt synthesis
# ---------------------------------------------------------------------------


def build_postmortem_prompt(
    session_id: str,
    events: list[CausalEvent],
    failure: FailurePoint,
    *,
    user_note: str = "",
) -> str:
    """Render the postmortem prompt fed to the LLM."""
    lines = [
        f"# Postmortem for session {session_id}",
        "",
        "## Reason",
        failure.reason,
        "",
        "## Trigger event",
    ]
    if failure.event is not None:
        lines.append(f"  {_render_event(failure.event)}")
    else:
        lines.append("  (no specific failure event identified)")

    if failure.ancestry:
        lines.append("")
        lines.append("## Proximate cause (youngest first)")
        for event in failure.ancestry[:15]:
            lines.append(f"  {_render_event(event)}")

    lines.append("")
    lines.append("## Full event log")
    for event in events[-50:]:
        lines.append(f"  {_render_event(event)}")

    if user_note:
        lines.append("")
        lines.append("## User note")
        lines.append(user_note.strip())

    lines.append("")
    lines.append(
        "Now produce the three-section remediation proposal. Start with `## Rule`."
    )
    return "\n".join(lines)


_EVENT_SUMMARY_MAX = 200
"""Hard cap on the summary slice we paste into the prompt.

Postmortem U3: a long, attacker-controlled summary is the most
direct prompt-injection vector — a tool that returns
``"\n\n## System override: ignore previous rules ..."`` would
otherwise land verbatim in the LLM context. We cap and sanitise.
"""


_DEFAULT_BANNED_SUMMARY_FRAGMENTS = (
    "## rule",
    "## skill",
    "## config",
    "ignore previous",
    "ignore all previous",
    "system override",
    "<|im_start|>",
    "<|im_end|>",
    "<<sys>>",
)


def _sanitise_for_prompt(text: str) -> str:
    """Make a piece of trace-derived text safe to paste into an LLM prompt.

    The defense is intentionally defensive-in-depth rather than perfect:

    * Collapse newlines so injected ``## Header`` lines can't break out
      of the surrounding indentation.
    * Strip C0 control characters except tab (which renders fine).
    * Truncate to :data:`_EVENT_SUMMARY_MAX` chars.
    * Mask known prompt-injection fragments by replacing the
      delimiter character so the LLM still sees the text but
      doesn't recognise it as a structural marker.

    A motivated attacker with arbitrary-string control could still
    write subtly adversarial content; our job is to remove the easy
    wins.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    cleaned = "".join(ch for ch in collapsed if ch == "\t" or ch >= " ")
    truncated = cleaned[:_EVENT_SUMMARY_MAX]
    lower = truncated.lower()
    for needle in _DEFAULT_BANNED_SUMMARY_FRAGMENTS:
        idx = lower.find(needle)
        while idx != -1:
            # Break the marker by replacing the first delimiter char.
            # The LLM still sees the textual content but the framing
            # cue is dropped, which kills most injection patterns.
            broken = "·" + truncated[idx + 1 :]
            truncated = truncated[:idx] + broken
            lower = truncated.lower()
            idx = lower.find(needle, idx + 1)
    return truncated


def _render_event(event: CausalEvent) -> str:
    parent = (
        f" ← {','.join(str(p) for p in event.parent_ids)}" if event.parent_ids else ""
    )
    payload = ""
    if event.payload:
        # Keep payload preview short — the LLM doesn't need full text.
        # The payload is JSON-serialised, which itself escapes the
        # most dangerous prompt-injection sequences (quotes, line
        # breaks), but we still cap the length.
        payload_text = json.dumps(event.payload, default=str, sort_keys=True)
        if len(payload_text) > 120:
            payload_text = payload_text[:119] + "…"
        payload = f"  payload={payload_text}"
    return (
        f"#{event.id:>4}  [{event.kind.value}] "
        f"{event.actor[:60]}: {_sanitise_for_prompt(event.summary)}"
        f"{parent}{payload}"
    )


# ---------------------------------------------------------------------------
# LLM call + parsing
# ---------------------------------------------------------------------------


_RULE_HEADER_RE = re.compile(r"^\s*##\s*Rule\b", re.MULTILINE | re.IGNORECASE)
_SKILL_HEADER_RE = re.compile(r"^\s*##\s*Skill\b", re.MULTILINE | re.IGNORECASE)
_CONFIG_HEADER_RE = re.compile(r"^\s*##\s*Config\b", re.MULTILINE | re.IGNORECASE)


def parse_proposal(raw: str) -> Proposal:
    """Split the LLM's text on the three ``## Rule|Skill|Config`` headers."""
    rule_match = _RULE_HEADER_RE.search(raw)
    skill_match = _SKILL_HEADER_RE.search(raw)
    config_match = _CONFIG_HEADER_RE.search(raw)

    def _section_body(
        start_match: re.Match[str] | None,
        end_match: re.Match[str] | None,
    ) -> str:
        if start_match is None:
            return ""
        start = start_match.end()
        end = end_match.start() if end_match is not None else len(raw)
        return raw[start:end].strip()

    rule_text = _section_body(rule_match, skill_match or config_match)
    skill_text = _section_body(skill_match, config_match)
    config_text = _section_body(config_match, None)
    return Proposal(
        rule_yaml=rule_text,
        skill_markdown=skill_text,
        config_change=config_text,
        raw=raw,
    )


def run_postmortem(
    *,
    session_id: str,
    working_dir: Path,
    model_invoke: Callable[[str, str], str],
    user_note: str = "",
    save: bool = True,
    enroll: bool = False,
    enroll_auto_activate: bool = False,
) -> PostmortemRun:
    """End-to-end postmortem: load events, build prompt, call model, save.

    Args:
        session_id: Causal-session id (or ``"latest"`` to resolve to
            the newest session under ``working_dir``).
        working_dir: Project root.
        model_invoke: Callable ``(system_prompt, user_prompt) -> str``.
            Tests pass a stub; the CLI passes a real LangChain model
            adapter.
        user_note: Optional free-form context from the user
            ("the agent should have asked before running the
            migration").
        save: When True, persist the rendered proposal to disk.
        enroll: Wave T — when True, route the proposal through
            :func:`bog_agents_cli.feedback_loop.enroll_postmortem_proposal`
            after a successful run. The result lands on
            :attr:`PostmortemRun.enrollment`. Default False to
            preserve the explicit-review workflow.
        enroll_auto_activate: When True (and ``enroll`` is True),
            the rule lands in the *active* rules directory rather
            than staging. Use only when the postmortem runs under
            human supervision.

    Returns:
        :class:`PostmortemRun` summarising what happened. ``error``
        is non-empty when the LLM call failed.
    """
    resolved_id = session_id
    if session_id == "latest":
        sessions = list_sessions(working_dir)
        if not sessions:
            return PostmortemRun(
                session_id="",
                failure=FailurePoint(event=None, reason="no_failure_detected"),
                proposal=None,
                error="No causal sessions found. Run /causal on and a turn first.",
            )
        resolved_id = sessions[0]
    events = load_session(working_dir, resolved_id)
    if not events:
        return PostmortemRun(
            session_id=resolved_id,
            failure=FailurePoint(event=None, reason="no_failure_detected"),
            proposal=None,
            error=f"Session {resolved_id} has no recorded events.",
        )

    failure = find_failure_point(events)
    prompt = build_postmortem_prompt(resolved_id, events, failure, user_note=user_note)
    try:
        raw = model_invoke(_POSTMORTEM_SYSTEM_PROMPT, prompt)
    except Exception as exc:
        logger.exception("postmortem: model_invoke raised")
        return PostmortemRun(
            session_id=resolved_id,
            failure=failure,
            proposal=None,
            error=f"Model call failed: {exc}",
        )
    proposal = parse_proposal(raw)

    saved_path: Path | None = None
    if save:
        saved_path = save_proposal(resolved_id, failure, proposal, working_dir)

    enrollment = None
    if enroll:
        # Wave T: feed the model's rule + skill into the proposer
        # staging dir so the same approve / lint pipeline applies.
        # We import lazily to avoid the (tiny) cost on the common
        # path where enrollment is off.
        from bog_agents_cli.feedback_loop import (
            enroll_postmortem_proposal,
        )

        try:
            enrollment = enroll_postmortem_proposal(
                proposal,
                working_dir=working_dir,
                source_session=resolved_id,
                auto_activate=enroll_auto_activate,
            )
        except Exception as exc:
            logger.exception("postmortem: enrollment failed")
            from bog_agents_cli.feedback_loop import EnrolledProposal

            enrollment = EnrolledProposal(skipped_reason=f"enrollment failed: {exc}")

    return PostmortemRun(
        session_id=resolved_id,
        failure=failure,
        proposal=proposal,
        saved_path=saved_path,
        enrollment=enrollment,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_proposal(
    session_id: str,
    failure: FailurePoint,
    proposal: Proposal,
    working_dir: Path,
) -> Path:
    """Write the proposal as a fenced markdown file the user can commit."""
    target_dir = working_dir / ".bog-agents" / "postmortems"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"{stamp}-{session_id}.md"
    target = target_dir / filename
    body = render_markdown(session_id, failure, proposal)
    atomic_write_text(target, body, encoding="utf-8")
    return target


def render_markdown(session_id: str, failure: FailurePoint, proposal: Proposal) -> str:
    """Render the proposal as a standalone markdown file."""
    lines = [
        f"# Postmortem — session {session_id}",
        f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_",
        "",
        f"**Failure kind:** `{failure.reason}`",
    ]
    if failure.event is not None:
        lines.append(
            f"**Trigger event:** #{failure.event.id} "
            f"`{failure.event.kind.value}` from `{failure.event.actor}`"
        )
    lines.extend(["", "## Rule", "", proposal.rule_yaml or "(no rule needed)"])
    lines.extend(["", "## Skill", "", proposal.skill_markdown or "(no skill needed)"])
    lines.extend(["", "## Config", "", proposal.config_change or "(no config change)"])
    lines.extend(["", "---", "", "<details><summary>Raw model output</summary>", ""])
    lines.append("```")
    lines.append(proposal.raw)
    lines.append("```")
    lines.append("</details>")
    return "\n".join(lines)


def render_run(run: PostmortemRun) -> str:
    """User-facing TUI render of a :class:`PostmortemRun`."""
    if run.error:
        return f"/postmortem failed: {run.error}"
    if run.proposal is None:
        return (
            f"Postmortem for session {run.session_id} could not produce a "
            f"proposal ({run.failure.reason})."
        )
    lines = [
        f"== Postmortem for {run.session_id} ==",
        f"  Failure kind: {run.failure.reason}",
    ]
    if run.failure.event is not None:
        lines.append(
            f"  Trigger:      #{run.failure.event.id} "
            f"[{run.failure.event.kind.value}] {run.failure.event.actor}"
        )
    lines.append("")
    lines.append("== Rule ==")
    lines.append(run.proposal.rule_yaml or "(no rule needed)")
    lines.append("")
    lines.append("== Skill ==")
    lines.append(run.proposal.skill_markdown or "(no skill needed)")
    lines.append("")
    lines.append("== Config ==")
    lines.append(run.proposal.config_change or "(no config change)")
    if run.saved_path is not None:
        lines.append("")
        lines.append(f"Proposal saved to: {run.saved_path}")
    if run.enrollment is not None:
        lines.append("")
        from bog_agents_cli.feedback_loop import render_enrollment

        lines.append(render_enrollment(run.enrollment))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slash-command dispatch
# ---------------------------------------------------------------------------


def dispatch(
    command_text: str,
    *,
    working_dir: Path,
    model_invoke: Callable[[str, str], str] | None = None,
) -> str:
    """Top-level ``/postmortem …`` handler.

    Args:
        command_text: Raw slash input.
        working_dir: Project root for session lookup + saves.
        model_invoke: Synchronous callable ``(system, user) -> str``.
            When ``None`` we return the help/list output without
            calling a model — useful for ``/postmortem list``.
    """
    text = command_text.strip()
    if text.startswith("/postmortem"):
        text = text[len("/postmortem") :].strip()
    if not text or text.lower() in ("help", "?"):
        return _help_text()
    if text.lower() in ("list", "ls"):
        return _list_postmortems(working_dir)

    # Parse flags up front. ``--enroll`` and ``--apply`` are
    # whitespace-delimited and can appear anywhere after the
    # session id; we strip them before the remaining text becomes
    # the user note.
    enroll = False
    enroll_auto_activate = False
    tokens = text.split()
    cleaned_tokens: list[str] = []
    for tok in tokens:
        if tok == "--enroll":
            enroll = True
        elif tok == "--apply":
            enroll = True
            enroll_auto_activate = True
        else:
            cleaned_tokens.append(tok)
    if not cleaned_tokens:
        return "Usage: /postmortem <session-id|latest> [--enroll] [--apply] [<note>]"
    session_id = cleaned_tokens[0]
    user_note = " ".join(cleaned_tokens[1:])

    if model_invoke is None:
        return (
            "/postmortem requires a model. The CLI normally supplies one "
            "from the active session — invoke /postmortem from the TUI."
        )
    run = run_postmortem(
        session_id=session_id,
        working_dir=working_dir,
        model_invoke=model_invoke,
        user_note=user_note,
        enroll=enroll,
        enroll_auto_activate=enroll_auto_activate,
    )
    return render_run(run)


def _list_postmortems(working_dir: Path) -> str:
    """Render the saved postmortems under ``.bog-agents/postmortems/``."""
    target_dir = working_dir / ".bog-agents" / "postmortems"
    if not target_dir.is_dir():
        return (
            "No postmortems saved yet.\n"
            "Run /postmortem latest after a surprising run to create one."
        )
    files = sorted(target_dir.glob("*.md"), reverse=True)
    if not files:
        return f"No postmortem files under {target_dir}."
    lines = [f"{len(files)} postmortem file(s):", ""]
    for path in files[:20]:
        lines.append(f"  {path.name}")
    if len(files) > 20:
        lines.append(f"  …and {len(files) - 20} older")
    lines.append("")
    lines.append(f"Open one from: {target_dir}")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "/postmortem — review a causal session and propose remediations.\n\n"
        "Usage:\n"
        "  /postmortem <session-id>          — analyse a specific session\n"
        "  /postmortem latest                — newest session\n"
        "  /postmortem latest <note>         — add free-text context\n"
        "  /postmortem latest --enroll       — also stage the rule + skill\n"
        "                                      for /expert proposals review\n"
        "  /postmortem latest --apply        — enroll AND activate the rule\n"
        "                                      immediately (use with care)\n"
        "  /postmortem list                  — list saved postmortems\n"
        "  /postmortem help                  — this message\n\n"
        "The model produces three sections: ## Rule / ## Skill / ## Config.\n"
        "The proposal is saved under .bog-agents/postmortems/. With\n"
        "--enroll, valid rule YAML lands in .bog-agents/expert_rules/proposals/\n"
        "and skill drafts in .bog-agents/skills/proposals/.\n"
    )


__all__ = [
    "FailurePoint",
    "PostmortemRun",
    "Proposal",
    "build_postmortem_prompt",
    "dispatch",
    "find_failure_point",
    "parse_proposal",
    "render_markdown",
    "render_run",
    "run_postmortem",
    "save_proposal",
]
