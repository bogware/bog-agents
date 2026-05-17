"""Audit runner (Wave R, R3).

Executes one :class:`AuditPack` against:

* the loaded :mod:`bog_agents_cli.expert_controller` rules
  (used by ``invariant`` and ``rule_presence`` / ``rule_absence``
  checks);
* the causal-trace log under ``<working_dir>/.bog-agents/causal/``
  (used by ``trace_assertion`` checks).

It then aggregates per-check :class:`CheckResult` records into an
:class:`AuditReport`.

The runner does **not** rely on the TUI or the LangGraph dev server.
It is callable from:

* the :mod:`bog_agents_cli.compliance.controller` slash dispatch;
* the daemon job recipe shipped under
  ``compliance/examples/audit-nightly.yaml`` (the daemon executes
  a small Python entrypoint that calls :func:`run_audit` with the
  resolved pack);
* a CI gate (`python -m bog_agents_cli.compliance ...` follow-up).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from bog_agents_cli.compliance.audit_pack import (
    AuditPack,
    Check,
    CheckKind,
)
from bog_agents_cli.compliance.evidence import (
    COLLECTORS,
    EvidenceFinding,
    TraceSlice,
    load_trace_slice,
    window_for_lookback,
)
from bog_agents_cli.compliance.report import (
    AuditReport,
    CheckResult,
    Evidence,
    Verdict,
)
from bog_agents_cli.policy_prove import ProofVerdict, prove

if TYPE_CHECKING:
    from bog_agents.middleware.expert_engine import Rule

logger = logging.getLogger(__name__)


def run_audit(
    pack: AuditPack,
    *,
    working_dir: Path,
    rules: Iterable[Rule] | None = None,
    now: float | None = None,
) -> AuditReport:
    """Execute ``pack`` end-to-end.

    Args:
        pack: The :class:`AuditPack` to evaluate.
        working_dir: Project root. Used to locate the causal log
            and (when ``rules`` is not supplied) the expert
            controller's loaded rules.
        rules: Pre-loaded rulebook. When ``None``, the runner asks
            the expert controller for the current rules; callers
            (tests, programmatic) inject directly to avoid that
            side-effect.
        now: Optional clock override (epoch seconds) — tests use
            it for deterministic windows.
    """
    started = time.time() if now is None else now
    rule_list = _resolve_rules(rules, working_dir)
    window = window_for_lookback(pack.window.lookback_hours, now=started)
    slice_ = load_trace_slice(working_dir, window)

    results: list[CheckResult] = []
    for check in pack.checks:
        results.append(_run_one(check, rules=rule_list, slice_=slice_))

    finished = time.time() if now is None else now
    return AuditReport(
        pack_name=pack.name,
        pack_version=pack.version,
        pack_source=str(pack.source_path) if pack.source_path else "<in-memory>",
        started_at=started,
        finished_at=finished,
        window=window,
        sessions_audited=slice_.sessions,
        results=tuple(results),
    )


# ---------------------------------------------------------------------------
# Per-check dispatch
# ---------------------------------------------------------------------------


def _run_one(
    check: Check, *, rules: list[Rule], slice_: TraceSlice
) -> CheckResult:
    if check.kind == CheckKind.INVARIANT:
        return _run_invariant(check, rules=rules)
    if check.kind == CheckKind.TRACE_ASSERTION:
        return _run_trace_assertion(check, slice_=slice_)
    if check.kind == CheckKind.RULE_PRESENCE:
        return _run_rule_presence(check, rules=rules)
    if check.kind == CheckKind.RULE_ABSENCE:
        return _run_rule_absence(check, rules=rules)
    # Shouldn't be reachable because AuditPack loader validated the
    # kind, but we keep the safety net for forward compatibility.
    return CheckResult(
        check_id=check.id,
        title=check.title,
        control=check.control,
        verdict=Verdict.INCONCLUSIVE,
        notes=(f"unsupported check kind: {check.kind.value}",),
    )


def _run_invariant(check: Check, *, rules: list[Rule]) -> CheckResult:
    if check.invariant is None:  # pragma: no cover — loader enforces
        return CheckResult(
            check_id=check.id,
            title=check.title,
            control=check.control,
            verdict=Verdict.INCONCLUSIVE,
            notes=("invariant check missing 'invariant' block",),
        )
    proof = prove(check.invariant, rules)
    if proof.verdict == ProofVerdict.HOLDS:
        verdict = Verdict.PASS
        observed = (
            f"Invariant {check.invariant.name!r} holds via "
            f"{len(proof.guards)} guard rule(s)"
        )
    elif proof.verdict == ProofVerdict.COUNTEREXAMPLE:
        verdict = Verdict.FAIL
        observed = (
            f"Invariant {check.invariant.name!r} violated — "
            "no guard rule blocks the forbidden pattern"
        )
    else:
        verdict = Verdict.INCONCLUSIVE
        observed = (
            f"Invariant {check.invariant.name!r} could not be decided"
        )
    evidence = Evidence(
        observed=observed,
        rationale=proof.rationale,
    )
    notes = proof.notes
    return CheckResult(
        check_id=check.id,
        title=check.title,
        control=check.control,
        verdict=verdict,
        evidence=(evidence,),
        notes=tuple(notes),
    )


def _run_trace_assertion(
    check: Check, *, slice_: TraceSlice
) -> CheckResult:
    if check.evidence is None:  # pragma: no cover — loader enforces
        return CheckResult(
            check_id=check.id,
            title=check.title,
            control=check.control,
            verdict=Verdict.INCONCLUSIVE,
            notes=("trace_assertion check missing 'evidence' block",),
        )
    collector = COLLECTORS.get(check.evidence.kind.value)
    if collector is None:  # pragma: no cover — loader enforces
        return CheckResult(
            check_id=check.id,
            title=check.title,
            control=check.control,
            verdict=Verdict.INCONCLUSIVE,
            notes=(
                f"no collector registered for evidence kind "
                f"{check.evidence.kind.value!r}",
            ),
        )
    finding: EvidenceFinding = collector(slice_, check.evidence.params)
    if finding.inconclusive:
        return CheckResult(
            check_id=check.id,
            title=check.title,
            control=check.control,
            verdict=Verdict.INCONCLUSIVE,
            evidence=(Evidence(observed="", rationale=finding.reason),),
            notes=(finding.reason,) if finding.reason else (),
        )
    sample_ids = tuple(e.id for e in finding.samples)
    evidence = Evidence(
        observed=finding.observed,
        samples=sample_ids,
    )
    verdict = Verdict.PASS if finding.passes else Verdict.FAIL
    return CheckResult(
        check_id=check.id,
        title=check.title,
        control=check.control,
        verdict=verdict,
        evidence=(evidence,),
    )


def _run_rule_presence(
    check: Check, *, rules: list[Rule]
) -> CheckResult:
    names = {r.name for r in rules}
    present = check.rule_name in names
    verdict = Verdict.PASS if present else Verdict.FAIL
    observed = (
        f"Rule {check.rule_name!r} is "
        f"{'loaded' if present else 'NOT loaded'}"
    )
    return CheckResult(
        check_id=check.id,
        title=check.title,
        control=check.control,
        verdict=verdict,
        evidence=(Evidence(observed=observed),),
    )


def _run_rule_absence(
    check: Check, *, rules: list[Rule]
) -> CheckResult:
    names = {r.name for r in rules}
    absent = check.rule_name not in names
    verdict = Verdict.PASS if absent else Verdict.FAIL
    observed = (
        f"Rule {check.rule_name!r} is "
        f"{'absent' if absent else 'present (should be removed)'}"
    )
    return CheckResult(
        check_id=check.id,
        title=check.title,
        control=check.control,
        verdict=verdict,
        evidence=(Evidence(observed=observed),),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_rules(
    rules: Iterable[Rule] | None, working_dir: Path
) -> list[Rule]:
    if rules is not None:
        return list(rules)
    try:
        from bog_agents_cli.expert_controller import get_controller

        return list(get_controller(working_dir).middleware.engine.rules)
    except Exception:
        logger.exception(
            "compliance: could not load expert rules; "
            "invariant / rule_presence checks will report INCONCLUSIVE"
        )
        return []


__all__ = ["run_audit"]
