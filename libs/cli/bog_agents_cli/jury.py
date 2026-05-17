"""``/jury`` — multi-reviewer diff vote.

Sends the same diff to N model "jurors" in parallel and aggregates their
verdicts. Each juror returns a structured judgement (approve / request
changes / reject) plus free-form feedback. The CLI then renders a
verdict matrix the user can scan in seconds.

The jurors come from ``[jury].models`` in ``~/.bog-agents/config.toml``.
If unset, the active model is used three times — useful as a quick
self-review while a real jury is being configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


JURY_SYSTEM_PROMPT = """\
You are a senior code reviewer on a multi-model jury. You will receive a
unified-diff patch and must return a single JSON object:

```
{
  "verdict": "approve" | "request_changes" | "reject",
  "summary": "<one sentence>",
  "issues": ["<short bullet>", ...],
  "score": <integer 0..10>
}
```

Rules:
- Be decisive. Empty issues list is fine for "approve".
- "request_changes" means the diff is on the right track but needs
  specific fixes before it ships.
- "reject" is for fundamentally wrong direction.
- "score" reflects overall quality (10 = ready to merge as-is).
- Output ONLY the JSON object — no prose, no markdown fence.
"""

VERDICT_APPROVE = "approve"
VERDICT_CHANGES = "request_changes"
VERDICT_REJECT = "reject"


@dataclass(frozen=True, slots=True)
class JurorVerdict:
    """One juror's verdict on a diff."""

    juror: str
    verdict: str
    summary: str
    issues: tuple[str, ...]
    score: int

    @property
    def is_valid(self) -> bool:
        """Whether the verdict parsed cleanly into a known label."""
        return self.verdict in (VERDICT_APPROVE, VERDICT_CHANGES, VERDICT_REJECT)


@dataclass(frozen=True, slots=True)
class JuryReport:
    """Aggregate result from a jury vote."""

    verdicts: tuple[JurorVerdict, ...]
    consensus: str
    avg_score: float

    def format_summary(self) -> str:
        """Render the report for inline display in the CLI."""
        if not self.verdicts:
            return "Jury was empty — no verdicts collected."
        lines: list[str] = [
            f"[bold]Jury verdict: {self.consensus.upper()}[/bold] "
            f"(avg score {self.avg_score:.1f}/10)\n"
        ]
        for v in self.verdicts:
            label_color = {
                VERDICT_APPROVE: "green",
                VERDICT_CHANGES: "yellow",
                VERDICT_REJECT: "red",
            }.get(v.verdict, "dim")
            lines.append(
                f"[{label_color}]{v.verdict:>15}[/{label_color}] "
                f"[cyan]{v.juror:<25}[/cyan] {v.score:>2}/10 — {v.summary}"
            )
            if v.issues:
                for issue in v.issues[:3]:
                    lines.append(f"        [dim]· {issue}[/dim]")
        return "\n".join(lines)


def load_jury_model_specs(config_path: Path | None = None) -> list[str]:
    """Read ``[jury].models`` from the config TOML.

    Returns:
        Empty list if no jury is configured.
    """
    import tomllib

    from bog_agents_cli.model_config import DEFAULT_CONFIG_PATH

    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    section = data.get("jury", {})
    if not isinstance(section, dict):
        return []
    raw = section.get("models", [])
    if not isinstance(raw, list):
        return []
    return [str(spec) for spec in raw if isinstance(spec, (str, int, float))]


_VERDICT_LABELS = {VERDICT_APPROVE, VERDICT_CHANGES, VERDICT_REJECT}
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_juror_response(juror: str, raw_text: str) -> JurorVerdict:
    """Extract a verdict from a juror's free-form response."""
    text = raw_text.strip()
    if not text:
        return JurorVerdict(
            juror=juror,
            verdict="invalid",
            summary="empty response",
            issues=(),
            score=0,
        )
    # Prefer the first JSON object in the response. Some models wrap their
    # JSON in commentary even when told not to.
    candidate = text
    match = _JSON_OBJECT_RE.search(text)
    if match is not None:
        candidate = match.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return JurorVerdict(
            juror=juror,
            verdict="invalid",
            summary=text[:200],
            issues=(),
            score=0,
        )
    if not isinstance(data, dict):
        return JurorVerdict(
            juror=juror,
            verdict="invalid",
            summary="non-object response",
            issues=(),
            score=0,
        )
    verdict_label = str(data.get("verdict", "")).strip().lower().replace("-", "_")
    if verdict_label not in _VERDICT_LABELS:
        verdict_label = "invalid"
    summary = str(data.get("summary", "")).strip() or "no summary"
    issues_raw = data.get("issues", [])
    issues = tuple(str(i) for i in issues_raw if isinstance(i, str))
    score_raw = data.get("score", 0)
    try:
        score = max(0, min(10, int(float(score_raw))))
    except (TypeError, ValueError):
        score = 0
    return JurorVerdict(
        juror=juror,
        verdict=verdict_label,
        summary=summary,
        issues=issues,
        score=score,
    )


def _consensus(verdicts: tuple[JurorVerdict, ...]) -> str:
    """Aggregate per-juror verdicts into a single consensus label.

    Strategy: any reject → reject; majority approve → approve; else
    request_changes. ``invalid`` verdicts are ignored for the purpose of
    consensus but counted in the report so the user knows about them.
    """
    valid = [v for v in verdicts if v.is_valid]
    if not valid:
        return "inconclusive"
    if any(v.verdict == VERDICT_REJECT for v in valid):
        return VERDICT_REJECT
    approve = sum(1 for v in valid if v.verdict == VERDICT_APPROVE)
    if approve > len(valid) / 2:
        return VERDICT_APPROVE
    return VERDICT_CHANGES


async def _run_one_juror(
    juror_id: str,
    model: BaseChatModel,
    diff_text: str,
) -> JurorVerdict:
    """Send the diff to one juror and parse the response."""
    from langchain_core.messages import HumanMessage, SystemMessage

    user = (
        "Review the following unified-diff patch and return your JSON verdict.\n\n"
        f"```diff\n{diff_text.strip()}\n```"
    )
    # L3: per-juror timeout. A hung model on one juror previously
    # blocked the whole report; jurors run in parallel under
    # gather() so each one needs its own cap.
    juror_timeout_s = 300.0
    try:
        response = await asyncio.wait_for(
            model.ainvoke(
                [SystemMessage(content=JURY_SYSTEM_PROMPT), HumanMessage(content=user)]
            ),
            timeout=juror_timeout_s,
        )
    except TimeoutError:
        logger.warning(
            "juror %s timed out after %.0fs", juror_id, juror_timeout_s
        )
        return JurorVerdict(
            juror=juror_id,
            verdict="invalid",
            summary=f"juror timed out after {juror_timeout_s:.0f}s",
            issues=(),
            score=0,
        )
    except Exception as exc:  # pragma: no cover — model errors are rare in tests
        logger.warning("juror %s failed: %s", juror_id, exc)
        return JurorVerdict(
            juror=juror_id,
            verdict="invalid",
            summary=f"juror call failed: {exc}",
            issues=(),
            score=0,
        )

    content = getattr(response, "content", "") or ""
    if isinstance(content, list):
        # Multimodal block list.
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    return _parse_juror_response(juror_id, str(content))


async def run_jury(
    diff_text: str,
    jurors: list[tuple[str, BaseChatModel]],
) -> JuryReport:
    """Run all jurors in parallel and aggregate their verdicts.

    Args:
        diff_text: The unified diff to review.
        jurors: List of ``(juror_label, model)`` pairs. The label is what
            shows up in the rendered report.

    Returns:
        A :class:`JuryReport` summarizing per-juror verdicts and consensus.

    Raises:
        ValueError: If ``jurors`` is empty or ``diff_text`` is blank.
    """
    if not diff_text or not diff_text.strip():
        msg = "run_jury() requires a non-empty diff_text"
        raise ValueError(msg)
    if not jurors:
        msg = "run_jury() requires at least one juror"
        raise ValueError(msg)

    tasks = [_run_one_juror(label, model, diff_text) for label, model in jurors]
    verdicts = tuple(await asyncio.gather(*tasks))

    valid = [v for v in verdicts if v.is_valid]
    avg_score = sum(v.score for v in valid) / len(valid) if valid else 0.0

    return JuryReport(
        verdicts=verdicts,
        consensus=_consensus(verdicts),
        avg_score=avg_score,
    )


__all__ = [
    "JURY_SYSTEM_PROMPT",
    "JurorVerdict",
    "JuryReport",
    "load_jury_model_specs",
    "run_jury",
]
