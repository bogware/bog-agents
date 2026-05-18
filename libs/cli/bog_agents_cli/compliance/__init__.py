"""Compliance auditor (Wave R).

A nightly compliance auditor for bog-agents. The user defines an
**audit pack** — a versioned YAML document listing controls and the
checks that exercise them — and the runner produces a signed
markdown report mapping each check to PASS / FAIL / N/A with
concrete evidence drawn from the causal trace log + the loaded
expert rulebook.

Public modules:

* :mod:`.audit_pack` — pack + check data models and YAML loader.
* :mod:`.evidence` — trace-scanning evidence collectors.
* :mod:`.runner` — runs an audit pack, produces an :class:`AuditReport`.
* :mod:`.report` — markdown renderer with HMAC-SHA-256 tamper seal.
* :mod:`.controller` — ``/audit`` slash-command facade.
* :mod:`.examples` — bundled example packs (SOC2 baseline).

Design points
-------------

* **Reproducible**. Given the same pack, the same rulebook, and the
  same trace window, the runner produces a byte-identical report
  (after stripping the wall-clock timestamp footer). The report id
  is content-addressed.
* **Tamper-evident**. Reports are sealed with HMAC-SHA-256 using a
  per-machine secret stored at ``~/.bog-agents/.audit-key`` (chmod
  0600 / icacls-restricted on Windows). A second run reading the
  same report verifies the seal.
* **Cron-friendly**. The runner is a single function call with no
  TUI dependency. The daemon ships a recipe (``audit-nightly.yaml``)
  the user can copy into their daemon config to schedule a run.
* **No silent skips**. A check that can't be evaluated (e.g. an
  invariant references a pattern with `op: missing` the heuristic
  prover can't sample) is reported as ``INCONCLUSIVE`` and counted
  separately from PASS / FAIL — auditors get to see the gap.
"""

from __future__ import annotations

from bog_agents_cli.compliance.audit_pack import (
    AuditPack,
    Check,
    CheckKind,
    EvidenceSpec,
    PackParseError,
    load_pack_from_dict,
    load_pack_from_yaml,
)
from bog_agents_cli.compliance.report import (
    AuditReport,
    CheckResult,
    Evidence,
    Verdict,
    render_markdown,
    seal_report,
    verify_seal,
)
from bog_agents_cli.compliance.runner import run_audit

__all__ = [
    "AuditPack",
    "AuditReport",
    "Check",
    "CheckKind",
    "CheckResult",
    "Evidence",
    "EvidenceSpec",
    "PackParseError",
    "Verdict",
    "load_pack_from_dict",
    "load_pack_from_yaml",
    "render_markdown",
    "run_audit",
    "seal_report",
    "verify_seal",
]
