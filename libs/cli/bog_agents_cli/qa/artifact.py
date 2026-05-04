"""QA artifact emitters — render an :class:`ExecutionResult` for humans/CI.

Supported formats:

- ``markdown`` — write a ``.md`` report file with summary table + details.
- ``json`` — write a structured ``.json`` file.
- ``stdout`` — return a text summary string (caller prints).
- ``jira-comment`` — render a Jira-friendly markdown comment string the
  caller posts via MCP.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bog_agents_cli.qa.executor import ExecutionResult
    from bog_agents_cli.qa.plan import QAPlan

logger = logging.getLogger(__name__)


def emit_artifact(
    plan: QAPlan,
    result: ExecutionResult,
    *,
    fmt: str,
    out_dir: Path | None = None,
) -> tuple[str, Path | None]:
    """Render ``result`` in the requested format.

    Args:
        plan: The plan that was executed.
        result: The execution result.
        fmt: One of ``"markdown"``, ``"json"``, ``"stdout"``,
            ``"jira-comment"``.
        out_dir: Required for file-output formats. Ignored otherwise.

    Returns:
        ``(text, path)`` where:

        - ``text`` is the rendered report string.
        - ``path`` is the output file path (None for ``stdout`` /
          ``jira-comment``).
    """
    fmt = (fmt or "markdown").lower()
    if fmt == "markdown":
        text = _render_markdown(plan, result)
        path = _write(out_dir, plan, result, "md", text) if out_dir else None
        return text, path
    if fmt == "json":
        text = json.dumps(_render_json_payload(plan, result), indent=2)
        path = _write(out_dir, plan, result, "json", text) if out_dir else None
        return text, path
    if fmt == "stdout":
        return _render_stdout_summary(plan, result), None
    if fmt == "jira-comment":
        return _render_jira_comment(plan, result), None
    msg = f"unknown artifact format: {fmt!r}"
    raise ValueError(msg)


def _write(
    out_dir: Path, plan: QAPlan, result: ExecutionResult, ext: str, text: str
) -> Path:
    plan_dir = out_dir / plan.plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / f"{result.run_id}.{ext}"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


_VERDICT_GLYPH = {"pass": "✅", "fail": "❌", "inconclusive": "⚠️"}


def _render_markdown(plan: QAPlan, result: ExecutionResult) -> str:
    parts: list[str] = []
    parts.append(f"# QA Report — {plan.name or plan.plan_id}")
    parts.append("")
    parts.append(f"- **Plan:** `{plan.plan_id}`")
    parts.append(f"- **Run:** `{result.run_id}`")
    parts.append(
        f"- **Started:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(result.started_at))}"
    )
    parts.append(f"- **Duration:** {result.duration_s:.2f}s")
    parts.append(
        f"- **Overall verdict:** {_VERDICT_GLYPH.get(result.overall_verdict, '?')} **{result.overall_verdict.upper()}**"
    )
    if result.aborted:
        parts.append("- **Aborted:** yes (a step with `on_fail: abort` failed)")
    parts.append("")
    parts.append("## Acceptance Criteria")
    parts.append("")
    parts.append("| AC | Verdict | Statement |")
    parts.append("|---|---|---|")
    for outcome in result.ac_outcomes:
        glyph = _VERDICT_GLYPH.get(outcome.verdict, "?")
        text = outcome.text.replace("\n", " ").strip()
        parts.append(f"| {outcome.ac_id} | {glyph} {outcome.verdict} | {text} |")
    parts.append("")
    parts.append("## Step Results")
    parts.append("")
    for step_result in result.step_results:
        glyph = "✅" if step_result.passed else "❌"
        parts.append(f"### {glyph} `{step_result.step_id}` — {step_result.kind}")
        parts.append("")
        if step_result.reason:
            parts.append(f"_Reason:_ {step_result.reason}")
            parts.append("")
        meta_bits: list[str] = []
        if step_result.exit_code is not None:
            meta_bits.append(f"exit_code={step_result.exit_code}")
        if step_result.status_code is not None:
            meta_bits.append(f"status={step_result.status_code}")
        meta_bits.append(f"duration={step_result.duration_s:.2f}s")
        parts.append(" · ".join(meta_bits))
        parts.append("")
        if step_result.output:
            parts.append("```")
            parts.append(step_result.output[:2000])
            parts.append("```")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _render_json_payload(plan: QAPlan, result: ExecutionResult) -> dict[str, Any]:
    return {
        "plan": {
            "id": plan.plan_id,
            "name": plan.name,
            "product": plan.product,
        },
        "result": result.to_dict(),
    }


def _render_stdout_summary(plan: QAPlan, result: ExecutionResult) -> str:
    lines = [
        f"QA: {plan.name or plan.plan_id}",
        f"  verdict: {result.overall_verdict.upper()}",
        f"  duration: {result.duration_s:.2f}s",
        f"  steps: {len(result.step_results)}",
    ]
    fails = [s for s in result.step_results if not s.passed]
    if fails:
        lines.append("  failed steps:")
        for s in fails:
            lines.append(f"    - {s.step_id} ({s.kind}): {s.reason}")
    lines.append("  AC outcomes:")
    for o in result.ac_outcomes:
        lines.append(f"    - {o.ac_id}: {o.verdict}")
    return "\n".join(lines)


def _render_jira_comment(plan: QAPlan, result: ExecutionResult) -> str:
    """Render a comment suitable for posting on a Jira issue.

    Jira accepts both plain text and limited markdown via the v3 ADF
    converter; this output is plain markdown which most Jira deployments
    render acceptably. The caller is expected to post via an MCP Jira
    tool.
    """
    glyph = _VERDICT_GLYPH.get(result.overall_verdict, "?")
    lines = [
        f"**QA Report — {plan.name or plan.plan_id}** {glyph} **{result.overall_verdict.upper()}**",
        "",
        f"Duration: {result.duration_s:.2f}s · Steps: {len(result.step_results)} · "
        f"Run: `{result.run_id}`",
        "",
        "| AC | Verdict | Statement |",
        "|---|---|---|",
    ]
    for outcome in result.ac_outcomes:
        text = outcome.text.replace("\n", " ").replace("|", "\\|").strip()
        lines.append(f"| {outcome.ac_id} | {outcome.verdict} | {text[:140]} |")
    fails = [s for s in result.step_results if not s.passed]
    if fails:
        lines.append("")
        lines.append("**Failures:**")
        for s in fails:
            lines.append(f"- `{s.step_id}` ({s.kind}): {s.reason}")
    return "\n".join(lines)
