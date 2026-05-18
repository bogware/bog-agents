"""Postmortem → dreamscape proposer bridge (Wave T).

The post-mortem flow (Q2) produces three artefacts when the model
emits a structured proposal:

* a YAML rule that *would have* prevented the failure,
* a markdown skill description,
* a one-line config change.

Today the user has to copy-paste the YAML into
``.bog-agents/expert_rules/proposals/`` themselves before
``/expert proposals approve`` can promote it. That step is friction
the trace → policy loop doesn't need.

This module closes the loop. :func:`enroll_postmortem_proposal`
takes one :class:`bog_agents_cli.postmortem.Proposal` and:

1. Strips the YAML fence the LLM tends to wrap around its rule
   block, runs it through the engine's loader + linter (same gates
   ``/expert write save`` uses).
2. When the rule parses and lints clean, writes it into
   ``<cwd>/.bog-agents/expert_rules/proposals/`` with a
   timestamped, traceable filename so successive postmortems don't
   clobber each other.
3. Optionally writes the skill markdown into a parallel staging
   directory under ``<cwd>/.bog-agents/skills/proposals/`` so the
   skills middleware reviewer flow can pick it up.

Failures are reported as :class:`EnrolledProposal.skipped_reason`
rather than raised — the caller almost always wants to surface the
reason inline next to the postmortem output.

Why this lives in a separate module and not inside ``postmortem.py``:

* Tests can exercise the bridge without spinning up an LLM (we
  hand it a synthesised :class:`Proposal`).
* The bridge has no business depending on the trace-mind or
  dreamscape packages directly — it only knows the on-disk layout
  the proposer + skills middleware already use.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from bog_agents.middleware.expert_engine.loader import (
    RuleLoadError,
    load_rules_from_string,
)

from bog_agents_cli.io_utils import atomic_write_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnrolledProposal:
    """Outcome of enrolling one postmortem proposal.

    Attributes:
        rule_saved_path: Where the rule YAML landed, or ``None`` when
            the proposal carried no usable rule.
        skill_saved_path: Where the skill markdown landed, or
            ``None`` when no usable skill was emitted.
        rules_parsed: How many :class:`Rule` objects the YAML
            produced when parsed. Zero when the rule wasn't saved.
        lint_errors: Any lint errors raised against the parsed rules.
            Non-empty implies the file was *not* saved.
        skipped_reason: When non-empty, the rule was not saved.
            Common reasons: ``"no rule needed"``, ``"empty proposal"``,
            ``"rule parse failed: …"``.
        active: True when ``auto_activate=True`` was honoured and the
            rule landed in the live rules directory rather than
            staging. Callers should trigger an engine reload when
            this is True.
        config_change: The proposal's config-change one-liner,
            surfaced unchanged so the caller can render it.
    """

    rule_saved_path: Path | None = None
    skill_saved_path: Path | None = None
    rules_parsed: int = 0
    lint_errors: tuple[str, ...] = ()
    skipped_reason: str = ""
    active: bool = False
    config_change: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """True iff at least one artefact landed on disk."""
        return self.rule_saved_path is not None or self.skill_saved_path is not None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_NO_RULE_MARKER = "(no rule needed)"
_NO_SKILL_MARKER = "(no skill needed)"
_NO_CONFIG_MARKER = "(no config change)"

# Default subdirectories — chosen to match the rest of the bog-agents
# on-disk convention so the existing /expert proposals + skills
# middleware reviewers pick the files up without further wiring.
_RULES_PROPOSALS_SUBDIR = Path(".bog-agents") / "expert_rules" / "proposals"
_RULES_ACTIVE_SUBDIR = Path(".bog-agents") / "expert_rules"
_SKILLS_PROPOSALS_SUBDIR = Path(".bog-agents") / "skills" / "proposals"

# Match a markdown fence the model commonly wraps around its YAML.
_YAML_FENCE_RE = re.compile(
    r"^\s*```(?:yaml|yml)?\s*\n(.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def enroll_postmortem_proposal(
    proposal,  # noqa: ANN001 — bog_agents_cli.postmortem.Proposal (circular import avoided)
    *,
    working_dir: Path,
    source_session: str = "",
    auto_activate: bool = False,
    proposals_dir: Path | None = None,
    rules_dir: Path | None = None,
    skills_dir: Path | None = None,
    now: float | None = None,
) -> EnrolledProposal:
    """Route a postmortem proposal into the rule / skill pipelines.

    Args:
        proposal: A :class:`bog_agents_cli.postmortem.Proposal`. We
            duck-type its ``rule_yaml`` / ``skill_markdown`` /
            ``config_change`` attributes so tests can pass any
            object with the same shape.
        working_dir: Project root. Used to anchor the default
            directories.
        source_session: The causal session id the postmortem was
            built from. Embedded into filenames so the proposal's
            origin is traceable.
        auto_activate: When True, write the rule directly into the
            *active* rules directory rather than the staging
            ``proposals/`` subdir. Default False to preserve the
            review-first workflow.
        proposals_dir: Override the staging dir for rule proposals.
            Default ``<cwd>/.bog-agents/expert_rules/proposals``.
        rules_dir: Override the active rules dir. Default
            ``<cwd>/.bog-agents/expert_rules``.
        skills_dir: Override the staging dir for skill markdown
            files. Default ``<cwd>/.bog-agents/skills/proposals``.
        now: Clock override (epoch seconds) — tests use it for
            deterministic filenames.

    Returns:
        :class:`EnrolledProposal` summarising what landed.
    """
    rule_yaml = _clean_text(getattr(proposal, "rule_yaml", ""))
    skill_md = _clean_text(getattr(proposal, "skill_markdown", ""))
    config = _clean_text(getattr(proposal, "config_change", ""))

    if not rule_yaml and not skill_md:
        return EnrolledProposal(
            skipped_reason="empty proposal — nothing to enroll",
            config_change=config,
        )

    notes: list[str] = []
    rule_path: Path | None = None
    skill_path: Path | None = None
    rules_parsed = 0
    lint_errors: tuple[str, ...] = ()
    rule_skipped: str = ""
    active = False

    if rule_yaml and not _is_no_rule_marker(rule_yaml):
        try:
            rule_path, rules_parsed, lint_errors, active = _save_rule(
                rule_yaml,
                working_dir=working_dir,
                source_session=source_session,
                auto_activate=auto_activate,
                proposals_dir=proposals_dir,
                rules_dir=rules_dir,
                now=now,
            )
            if rule_path is not None:
                notes.append(f"Rule staged at {rule_path}")
            elif lint_errors:
                rule_skipped = (
                    f"rule lint errors prevented save: {'; '.join(lint_errors)[:200]}"
                )
        except RuleLoadError as exc:
            rule_skipped = f"rule parse failed: {exc}"
            notes.append(rule_skipped)
    elif rule_yaml:
        rule_skipped = "rule proposal was '(no rule needed)' — skipping"

    if skill_md and not _is_no_skill_marker(skill_md):
        try:
            skill_path = _save_skill(
                skill_md,
                working_dir=working_dir,
                source_session=source_session,
                skills_dir=skills_dir,
                now=now,
            )
            notes.append(f"Skill draft saved at {skill_path}")
        except OSError as exc:
            notes.append(f"could not write skill draft: {exc}")

    skipped_reason = ""
    if rule_skipped and not rule_path:
        skipped_reason = rule_skipped

    return EnrolledProposal(
        rule_saved_path=rule_path,
        skill_saved_path=skill_path,
        rules_parsed=rules_parsed,
        lint_errors=lint_errors,
        skipped_reason=skipped_reason,
        active=active,
        config_change=config,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Internals — rule pipeline
# ---------------------------------------------------------------------------


def _save_rule(
    yaml_text: str,
    *,
    working_dir: Path,
    source_session: str,
    auto_activate: bool,
    proposals_dir: Path | None,
    rules_dir: Path | None,
    now: float | None,
) -> tuple[Path | None, int, tuple[str, ...], bool]:
    """Strip + parse + lint + write the rule YAML.

    Returns:
        ``(saved_path, rules_parsed, lint_errors, active)``.
        ``saved_path`` is ``None`` when the YAML failed to lint
        cleanly. Lint *errors* (not warnings) gate the save.
    """
    body = _strip_yaml_fence(yaml_text)
    rules = load_rules_from_string(body, source="<postmortem>")
    rules_parsed = len(rules)
    if rules_parsed == 0:
        return (None, 0, ("YAML parsed but produced zero rules",), False)

    lint_errors = _lint_errors(rules)
    if lint_errors:
        return (None, rules_parsed, lint_errors, False)

    target_dir = (
        rules_dir or (working_dir / _RULES_ACTIVE_SUBDIR)
        if auto_activate
        else proposals_dir or (working_dir / _RULES_PROPOSALS_SUBDIR)
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / _rule_filename(rules, source_session, now)
    # Don't clobber an existing on-disk rule silently. The proposer
    # already enforces this for auto_activate; we extend the same
    # guard to staged proposals so a postmortem can't masquerade as
    # an earlier one.
    if target_path.exists():
        target_path = _disambiguate_filename(target_path)
    atomic_write_text(target_path, body, encoding="utf-8")
    return (target_path, rules_parsed, (), auto_activate)


def _lint_errors(rules) -> tuple[str, ...]:  # noqa: ANN001 — list[Rule]
    """Run the engine's lint and return error-severity messages only."""
    try:
        from bog_agents.middleware.expert_engine import lint as run_lint
    except ImportError:  # pragma: no cover — package shape
        return ()
    report = run_lint(rules)
    return tuple(_iter_lint_errors(report))


def _iter_lint_errors(report) -> list[str]:  # noqa: ANN001 — LintReport
    errors: list[str] = []
    # LintReport exposes either ``errors`` (list[LintIssue]) or, in
    # older revisions, ``items`` we filter by severity. Cover both.
    candidates = getattr(report, "errors", None) or [
        item
        for item in getattr(report, "items", ())
        if getattr(item, "severity", "").lower() == "error"
    ]
    for item in candidates or ():
        message = getattr(item, "message", None) or getattr(item, "text", None)
        if not message:
            message = str(item)
        rule_name = getattr(item, "rule_name", "") or getattr(item, "rule", "")
        errors.append(f"{rule_name}: {message}" if rule_name else str(message))
    return errors


def _rule_filename(
    rules,  # noqa: ANN001 — list[Rule]
    source_session: str,
    now: float | None,
) -> str:
    """Build a stable, traceable filename for a rule proposal."""
    primary = getattr(rules[0], "name", "rule") or "rule"
    stamp = time.strftime(
        "%Y%m%dT%H%M%SZ",
        time.gmtime(now if now is not None else time.time()),
    )
    session_tag = f"-{source_session[:12]}" if source_session else ""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", primary).strip("-").lower()
    return f"postmortem-{stamp}{session_tag}-{safe_name}.yaml"


def _disambiguate_filename(path: Path) -> Path:
    """Append ``-N`` until the path doesn't exist (cap N at 99)."""
    stem = path.stem
    suffix = path.suffix
    for n in range(1, 100):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    return path  # give up; caller writes will overwrite


# ---------------------------------------------------------------------------
# Internals — skill pipeline
# ---------------------------------------------------------------------------


def _save_skill(
    markdown: str,
    *,
    working_dir: Path,
    source_session: str,
    skills_dir: Path | None,
    now: float | None,
) -> Path:
    """Write a skill markdown draft for the skills-reviewer flow."""
    target_dir = skills_dir or (working_dir / _SKILLS_PROPOSALS_SUBDIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime(
        "%Y%m%dT%H%M%SZ",
        time.gmtime(now if now is not None else time.time()),
    )
    session_tag = f"-{source_session[:12]}" if source_session else ""
    target = target_dir / f"postmortem-{stamp}{session_tag}.md"
    if target.exists():
        target = _disambiguate_filename(target)
    body = (
        f"# Skill draft from postmortem\n\n"
        f"_Source session: `{source_session or '<unknown>'}`_\n\n"
        f"{markdown.strip()}\n"
    )
    atomic_write_text(target, body, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_text(value: object) -> str:
    if not value:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    return value.strip()


def _is_no_rule_marker(text: str) -> bool:
    return text.strip().lower().startswith(_NO_RULE_MARKER.lower())


def _is_no_skill_marker(text: str) -> bool:
    return text.strip().lower().startswith(_NO_SKILL_MARKER.lower())


def _strip_yaml_fence(text: str) -> str:
    """Return the YAML body, stripping a leading ```yaml fence if present."""
    match = _YAML_FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_enrollment(enrolled: EnrolledProposal) -> str:
    """Format an :class:`EnrolledProposal` for the TUI."""
    lines = ["== Postmortem enrolled =="]
    if enrolled.rule_saved_path is not None:
        state = (
            "ACTIVE (will fire on next reload)"
            if enrolled.active
            else "STAGED for review"
        )
        lines.append(f"  Rule:     {enrolled.rule_saved_path}  [{state}]")
        lines.append(f"            ({enrolled.rules_parsed} rule(s) parsed)")
    if enrolled.skill_saved_path is not None:
        lines.append(f"  Skill:    {enrolled.skill_saved_path}")
    if enrolled.config_change and not _is_no_config_marker(enrolled.config_change):
        lines.append(f"  Config:   {enrolled.config_change}")
    if enrolled.lint_errors:
        lines.append("  Lint errors:")
        for err in enrolled.lint_errors:
            lines.append(f"    - {err}")
    if enrolled.skipped_reason:
        lines.append(f"  Skipped:  {enrolled.skipped_reason}")
    if enrolled.notes:
        lines.append("  Notes:")
        for note in enrolled.notes:
            lines.append(f"    · {note}")
    if not (
        enrolled.rule_saved_path or enrolled.skill_saved_path or enrolled.skipped_reason
    ):
        lines.append("  (nothing enrolled — postmortem produced no usable artefacts)")
    return "\n".join(lines)


def _is_no_config_marker(text: str) -> bool:
    return text.strip().lower().startswith(_NO_CONFIG_MARKER.lower())


__all__ = [
    "EnrolledProposal",
    "enroll_postmortem_proposal",
    "render_enrollment",
]
