"""Persisted operator decisions, the bias they feed back, and the `/cost` counterfactual (ROADMAP #53).

Every routing decision the operator makes is appended to
`~/.bog-agents/operator-decisions.jsonl` with the judged tier, the tier the
objective turned it into, the models on both sides and — once the turn ends —
its token counts and cost. Two things read it back: `bias()` stops the `cost`
objective from downgrading a tier whose downgrades the user keeps ruling
`bad` (`/operator verdict bad`), and `counterfactual()` prices what the same
tokens would have cost on the judged tier's model, which is the "saved $X by
routing N turns below the judged tier" line in `/cost`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

OBJECTIVES: tuple[str, ...] = ("intelligence", "balance", "cost")
TIER_ORDER: tuple[str, ...] = ("easy", "medium", "hard", "max")
_MAX_LINES = 2000
_LOCAL_PREFIXES = ("ollama:", "local:", "lmstudio:", "llamacpp:", "vllm:")


def apply_objective(
    tier: str, objective: str, *, blocked: frozenset[str] = frozenset()
) -> str:
    """The tier to run after the objective: `cost` steps down one tier, `intelligence` steps up, `balance` keeps it.

    Args:
        tier: The judge's tier.
        objective: One of `OBJECTIVES`.
        blocked: Tiers the bias says must not be downgraded (their downgrades kept failing).
    """
    if tier not in TIER_ORDER:
        return tier
    index = TIER_ORDER.index(tier)
    if objective == "cost" and index > 0 and tier not in blocked:
        return TIER_ORDER[index - 1]
    if objective == "intelligence" and index < len(TIER_ORDER) - 1:
        return TIER_ORDER[index + 1]
    return tier


def is_local_model(spec: str) -> bool:
    """Whether a model spec names a locally hosted model (no per-token bill)."""
    return spec.lower().startswith(_LOCAL_PREFIXES)


def estimate_cost_usd(spec: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD for these tokens on `spec` from the SDK price catalog; local models cost 0; unknown → `None`."""
    if is_local_model(spec):
        return 0.0
    from bog_agents.middleware.cost_tracker import price_for_model

    name = spec.split(":", 1)[1] if ":" in spec else spec
    prices = price_for_model(name)
    if prices is None:
        return None
    return (input_tokens / 1_000_000) * prices[0] + (
        output_tokens / 1_000_000
    ) * prices[1]


@dataclass
class DecisionRecord:
    """One line of the decisions log."""

    decision_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: float = field(default_factory=time.time)
    judged_tier: str = ""
    tier: str = ""
    objective: str = "balance"
    model: str = ""
    judged_model: str = ""
    effort: str = ""
    route: str = "direct"
    prompt_preview: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    verdict: str = ""
    note: str = ""

    @property
    def downgraded(self) -> bool:
        """Whether the objective sent this turn below the judged tier."""
        return (
            self.judged_tier in TIER_ORDER
            and self.tier in TIER_ORDER
            and TIER_ORDER.index(self.tier) < TIER_ORDER.index(self.judged_tier)
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DecisionRecord:
        """Build from a stored mapping, ignoring unknown keys."""
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]


def decisions_path() -> Path:
    """`~/.bog-agents/operator-decisions.jsonl`."""
    from bog_agents_cli.feature_helpers import feature_state_dir

    return feature_state_dir() / "operator-decisions.jsonl"


def load_decisions(path: Path | None = None) -> list[DecisionRecord]:
    """Every record in the log (oldest first); unreadable lines are skipped."""
    target = path or decisions_path()
    if not target.is_file():
        return []
    records: list[DecisionRecord] = []
    try:
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(DecisionRecord.from_dict(json.loads(line)))
            except (ValueError, TypeError):
                continue
    except OSError:
        logger.debug("Could not read %s", target, exc_info=True)
    return records


def _write_all(records: list[DecisionRecord], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(r.to_dict()) + "\n" for r in records[-_MAX_LINES:]),
        encoding="utf-8",
    )
    tmp.replace(target)


def record_decision(record: DecisionRecord, path: Path | None = None) -> DecisionRecord:
    """Append a decision (best effort) and return it."""
    target = path or decisions_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_dict()) + "\n")
        if sum(1 for _ in target.open(encoding="utf-8")) > _MAX_LINES:
            _write_all(load_decisions(target), target)
    except OSError:
        logger.debug("Could not record the operator decision", exc_info=True)
    return record


def update_decision(
    decision_id: str, path: Path | None = None, **fields: object
) -> bool:
    """Set fields on one record (by id); `False` when it is not in the log."""
    target = path or decisions_path()
    records = load_decisions(target)
    for record in reversed(records):
        if record.decision_id == decision_id:
            for key, value in fields.items():
                if key in record.__dataclass_fields__:
                    setattr(record, key, value)
            try:
                _write_all(records, target)
            except OSError:
                logger.debug("Could not update the operator decision", exc_info=True)
                return False
            return True
    return False


def bias(
    path: Path | None = None, *, min_samples: int = 3, bad_ratio: float = 0.5
) -> frozenset[str]:
    """Tiers the `cost` objective must stop downgrading: their downgrades were ruled `bad` too often."""
    counts: dict[str, list[int]] = {}
    for record in load_decisions(path):
        if not record.downgraded or record.verdict not in ("good", "bad"):
            continue
        totals = counts.setdefault(record.judged_tier, [0, 0])
        totals[0] += 1
        if record.verdict == "bad":
            totals[1] += 1
    return frozenset(
        tier
        for tier, (total, bad) in counts.items()
        if total >= min_samples and bad / total >= bad_ratio
    )


def counterfactual(path: Path | None = None) -> tuple[float, int, int]:
    """`(saved_usd, routed_turns, local_turns)` over the priced downgraded / local decisions."""
    saved = 0.0
    routed = 0
    local = 0
    for record in load_decisions(path):
        if record.cost_usd is None or not (record.input_tokens or record.output_tokens):
            continue
        if record.model != record.judged_model or record.downgraded:
            baseline = estimate_cost_usd(
                record.judged_model, record.input_tokens, record.output_tokens
            )
            if baseline is not None:
                saved += max(0.0, baseline - record.cost_usd)
                routed += 1
                if is_local_model(record.model):
                    local += 1
    return saved, routed, local


def counterfactual_line(path: Path | None = None) -> str | None:
    """The `/cost` line, or `None` when nothing was routed below the judged tier yet."""
    saved, routed, local = counterfactual(path)
    if not routed:
        return None
    local_text = f", {local} to local models" if local else ""
    return f"Operator: saved ${saved:.2f} by routing {routed} turn(s) below the judged tier{local_text} (/operator status)"


__all__ = [
    "OBJECTIVES",
    "TIER_ORDER",
    "DecisionRecord",
    "apply_objective",
    "bias",
    "counterfactual",
    "counterfactual_line",
    "decisions_path",
    "estimate_cost_usd",
    "is_local_model",
    "load_decisions",
    "record_decision",
    "update_decision",
]
