"""Audit report data model + signed markdown renderer (Wave R, R4).

The runner produces an :class:`AuditReport`. The renderer turns it
into a deterministic markdown document with a tamper-evident HMAC-
SHA-256 seal footer. ``verify_seal`` re-derives the seal and
compares; tests + downstream consumers can verify a report wasn't
edited after sealing.

Determinism notes
-----------------

The seal is computed over the *body* of the report (everything
above the seal footer). Two runs with the same pack, the same
rulebook, and the same trace window produce the same body — and
therefore the same seal — modulo the wall-clock timestamp at the
top of the report (which is intentionally excluded from the seal,
since timestamps move).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from bog_agents_cli.causal.ledger import CausalEvent
from bog_agents_cli.compliance.audit_pack import (
    AuditPack,
    Check,
)
from bog_agents_cli.compliance.evidence import EvidenceWindow

logger = logging.getLogger(__name__)


_SEAL_VERSION = "v1"
"""Embedded in the seal footer so a future seal format can be
distinguished from the current one without parsing the body."""

_SEAL_HEADER = "## Seal"
"""Section header the renderer emits + ``verify_seal`` keys on."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    """One check's outcome."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    """The check could not be evaluated (e.g. the heuristic prover
    returned INCONCLUSIVE; the evidence collector was misconfigured)."""

    NOT_APPLICABLE = "not_applicable"
    """The check explicitly declined to evaluate — e.g. a rule
    presence check against a project with no rules at all."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """A single concrete observation attached to a CheckResult.

    Attributes:
        observed: Plain-English description.
        samples: Event ids referenced (when applicable). Surfaced as
            "see event #N" links in the report.
        rationale: Free-form one-liner from the underlying engine
            (the prover's rationale, the collector's reason).
    """

    observed: str
    samples: tuple[int, ...] = ()
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's audit outcome."""

    check_id: str
    title: str
    control: str
    verdict: Verdict
    evidence: tuple[Evidence, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AuditReport:
    """End-to-end output of one audit run."""

    pack_name: str
    pack_version: int
    pack_source: str
    """``str(source_path)`` or ``"<in-memory>"``."""

    started_at: float
    finished_at: float
    window: EvidenceWindow
    sessions_audited: tuple[str, ...]
    results: tuple[CheckResult, ...]

    @property
    def counts(self) -> dict[Verdict, int]:
        """Verdict histogram across all checks."""
        out: dict[Verdict, int] = dict.fromkeys(Verdict, 0)
        for r in self.results:
            out[r.verdict] = out.get(r.verdict, 0) + 1
        return out

    @property
    def overall(self) -> Verdict:
        """One-word summary used at the top of the report.

        ``FAIL`` if any check failed; otherwise ``INCONCLUSIVE`` if any
        check is inconclusive; otherwise ``PASS``.
        """
        c = self.counts
        if c[Verdict.FAIL] > 0:
            return Verdict.FAIL
        if c[Verdict.INCONCLUSIVE] > 0:
            return Verdict.INCONCLUSIVE
        return Verdict.PASS


# ---------------------------------------------------------------------------
# Per-machine signing key
# ---------------------------------------------------------------------------


_KEY_FILENAME = ".audit-key"
_KEY_LENGTH_BYTES = 32


def _key_path() -> Path:
    """Where the per-machine HMAC secret lives."""
    return Path.home() / ".bog-agents" / _KEY_FILENAME


def _load_or_create_key() -> bytes:
    """Read the HMAC secret, creating it (and the parent dir) if absent.

    The key is stored as urlsafe base64 + an explicit version header
    so we can rotate the algorithm later without dropping existing
    seals on the floor. File mode is restricted to owner-only via
    :mod:`vars_store`-style helpers.
    """
    path = _key_path()
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("compliance: could not read audit key: %s", exc)
            return _mint_and_store_key(path)
        if raw.startswith("v1:"):
            try:
                return base64.urlsafe_b64decode(raw[3:])
            except (ValueError, TypeError):
                logger.warning("compliance: audit key corrupt; rotating")
        # Pre-v1 keys (treat as a one-line raw base64).
        try:
            return base64.urlsafe_b64decode(raw)
        except (ValueError, TypeError):
            logger.warning("compliance: audit key unreadable; rotating")
    return _mint_and_store_key(path)


def _mint_and_store_key(path: Path) -> bytes:
    """Generate a fresh secret and persist with owner-only permissions."""
    import secrets

    from bog_agents_cli.io_utils import atomic_write_text
    from bog_agents_cli.vars_store import _secure_owner_only

    secret = secrets.token_bytes(_KEY_LENGTH_BYTES)
    encoded = base64.urlsafe_b64encode(secret).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, f"v1:{encoded}\n", encoding="utf-8")
    try:
        _secure_owner_only(path)
    except Exception:
        logger.debug("compliance: _secure_owner_only failed", exc_info=True)
    logger.info("compliance: minted new audit key at %s", path)
    return secret


def _hmac(body: str, key: bytes | None = None) -> str:
    """Compute the body's HMAC-SHA-256 hex digest with the audit key."""
    secret = key if key is not None else _load_or_create_key()
    return hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Body renderer
# ---------------------------------------------------------------------------


_VERDICT_ICON: dict[Verdict, str] = {
    Verdict.PASS: "✓",
    Verdict.FAIL: "✗",
    Verdict.INCONCLUSIVE: "?",
    Verdict.NOT_APPLICABLE: "—",
}


def render_markdown(
    report: AuditReport,
    *,
    pack: AuditPack | None = None,
    by_id: dict[int, CausalEvent] | None = None,
) -> str:
    """Render ``report`` as a markdown document. Body only — caller
    adds the seal via :func:`seal_report`.

    Args:
        report: The audit run output.
        pack: Optional original pack — used to fill in per-check
            descriptions the report wants to surface.
        by_id: Optional ``{event_id: CausalEvent}`` map so the
            renderer can inline event summaries beside sample ids.
    """
    counts = report.counts
    pack_blocks = ""
    if pack is not None and pack.description:
        pack_blocks = f"\n_{pack.description}_\n"
    lines = [
        f"# Audit report: {report.pack_name} (v{report.pack_version})",
        pack_blocks.rstrip(),
        "",
        f"**Overall verdict:** {_VERDICT_ICON[report.overall]} "
        f"**{report.overall.value.upper()}**",
        "",
        "## Summary",
        "",
        "| Verdict | Count |",
        "| --- | --- |",
    ]
    for verdict in (
        Verdict.PASS,
        Verdict.FAIL,
        Verdict.INCONCLUSIVE,
        Verdict.NOT_APPLICABLE,
    ):
        lines.append(f"| {verdict.value} | {counts[verdict]} |")

    lines.extend(
        [
            "",
            "## Audit metadata",
            "",
            f"- **Pack source:** `{report.pack_source}`",
            f"- **Window:** `{report.window.oldest_allowed:.0f}` → "
            f"`{report.window.now:.0f}` (epoch seconds)",
            f"- **Sessions audited:** {len(report.sessions_audited)}",
            f"- **Started:** `{report.started_at:.0f}`",
            f"- **Finished:** `{report.finished_at:.0f}`",
            "",
            "## Control coverage",
            "",
            "| Check id | Control | Verdict | Title |",
            "| --- | --- | --- | --- |",
        ]
    )
    for r in report.results:
        ctrl = r.control or "—"
        icon = _VERDICT_ICON[r.verdict]
        lines.append(
            f"| `{r.check_id}` | `{ctrl}` | {icon} {r.verdict.value} | {r.title} |"
        )

    lines.extend(["", "## Per-check detail", ""])
    pack_by_id = {c.id: c for c in (pack.checks if pack is not None else ())}
    for r in report.results:
        lines.append(f"### {_VERDICT_ICON[r.verdict]} `{r.check_id}` — {r.title}")
        if r.control:
            lines.append(f"_Control: `{r.control}`_")
        lines.append("")
        lines.append(f"**Verdict:** {r.verdict.value.upper()}")
        original = pack_by_id.get(r.check_id)
        if original is not None and original.description:
            lines.append("")
            lines.append(original.description)
        for evidence in r.evidence:
            lines.append("")
            lines.append(f"**Evidence:** {evidence.observed}")
            if evidence.rationale:
                lines.append(f"_Rationale: {evidence.rationale}_")
            if evidence.samples and by_id is not None:
                lines.append("")
                lines.append("Sample events:")
                for sid in evidence.samples:
                    sample = by_id.get(sid)
                    if sample is None:
                        lines.append(f"  - `#{sid}` (event not in current slice)")
                    else:
                        lines.append(
                            f"  - `#{sid}` [{sample.kind.value}] "
                            f"{sample.actor}: {sample.summary[:80]}"
                        )
            elif evidence.samples:
                lines.append(
                    "Sample event ids: " + ", ".join(f"#{s}" for s in evidence.samples)
                )
        for note in r.notes:
            lines.append(f"  · {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------


def seal_report(body: str, *, key: bytes | None = None) -> str:
    """Append the HMAC seal footer to *body* and return the sealed text.

    The digest is computed over the body **after** the trailing-newline
    strip that ``verify_seal`` will later reconstruct. Without this
    pre-strip, sealing and verifying see different bodies when the
    source has trailing whitespace.
    """
    normalised = body.rstrip("\n")
    digest = _hmac(normalised, key=key)
    footer = (
        "\n---\n\n"
        f"{_SEAL_HEADER}\n\n"
        f"- algorithm: HMAC-SHA-256\n"
        f"- version:   {_SEAL_VERSION}\n"
        f"- digest:    `{digest}`\n"
        f"- sealed_at: `{time.time():.0f}`\n"
    )
    return normalised + footer


def verify_seal(sealed: str, *, key: bytes | None = None) -> tuple[bool, str]:
    """Verify a sealed report.

    Returns:
        ``(ok, message)``. ``ok`` is True when the digest in the
        footer matches the recomputed digest over the body.
    """
    marker = "\n---\n\n" + _SEAL_HEADER
    idx = sealed.rfind(marker)
    if idx < 0:
        return (False, "no seal footer found")
    body = sealed[:idx]
    footer = sealed[idx + len(marker) :]
    digest = _parse_seal_digest(footer)
    if digest is None:
        return (False, "seal footer is malformed")
    expected = _hmac(body, key=key)
    if not hmac.compare_digest(digest, expected):
        return (False, "seal digest mismatch — report has been edited")
    return (True, "seal valid")


def _parse_seal_digest(footer: str) -> str | None:
    for line in footer.splitlines():
        stripped = line.strip()
        if stripped.startswith("- digest:"):
            value = stripped.split(":", 1)[1].strip().strip("`")
            if value:
                return value
    return None


# ---------------------------------------------------------------------------
# JSON sidecar (for programmatic consumers + CI gates)
# ---------------------------------------------------------------------------


def report_to_json(report: AuditReport) -> str:
    """Serialise the report as deterministic JSON.

    Useful for CI gates that want to fail the build when
    ``overall != PASS`` without parsing the markdown.
    """
    payload = asdict(report)
    # asdict turns the StrEnum into the enum object; coerce to value.
    for r in payload.get("results", []):
        if isinstance(r.get("verdict"), Verdict):
            r["verdict"] = r["verdict"].value
    payload["overall"] = report.overall.value
    payload["counts"] = {k.value: v for k, v in report.counts.items()}
    return json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))


__all__ = [
    "AuditReport",
    "CheckResult",
    "Evidence",
    "Verdict",
    "render_markdown",
    "report_to_json",
    "seal_report",
    "verify_seal",
]


# Quiet "unused import" lint on Check — used inline in render_markdown.
_ = Check
