#!/usr/bin/env python3
# ruff: noqa: ANN401, T201, D103, PLR1714, PLC1901
"""Regenerate ``docs/dreamscape-runs/trends.md`` from the phase JSON snapshots.

Each ``docs/dreamscape-runs/phase-NNN-YYYY-MM-DD.json`` describes one
real-world dreamscape test run. The cross-phase ``trends.md`` had been
hand-maintained — by Phase 7 it was 100+ lines spanning five tables.
Phase 8 closes that loop by sourcing the tables from the JSON files
directly, so adding a new phase needs no manual table-editing.

The JSON shapes drifted over time (P1-P2 used a scenarios array;
P3-P7 use flatter scheduler-centric shapes), so this module's
:func:`extract_summary` accepts heterogeneous inputs and pulls only
the fields that exist into a normalized ``PhaseSummary`` record. Any
field that's missing in a given phase renders as ``n/a`` in the
output tables.

Run from the repo root::

    python scripts/build_dreamscape_trends.py

Or via ``--check`` to fail the build if ``trends.md`` is stale::

    python scripts/build_dreamscape_trends.py --check

The script is idempotent — running it twice in a row produces no diff.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASE_DIR = REPO_ROOT / "docs" / "dreamscape-runs"
TRENDS_FILE = PHASE_DIR / "trends.md"


# ---------------------------------------------------------------------------
# Normalized phase summary
# ---------------------------------------------------------------------------


@dataclass
class PhaseSummary:
    """One row's worth of cross-phase trend data, normalized.

    Every field is optional because the underlying JSON shapes drifted
    between Phases 1-2 (scenario-based) and Phases 3-7 (flatter,
    scheduler-centric). Missing fields render as ``n/a``.
    """

    phase: int = 0
    date: str = ""
    test_focus: str = ""
    verdict: str = ""

    # Cost + duration
    llm_calls: int | None = None
    wall_seconds: float | None = None
    cost_usd: float | None = None

    # Counters
    dreams_fired: int | None = None
    skipped_ineligible: int | None = None
    errors: int | None = None
    is_running_at_end: bool | None = None
    unique_titles: int | None = None
    unique_titles_total_seen: int | None = None
    avg_seconds_per_dream: float | None = None

    # Test counts
    dreamscape_unit_tests: int | None = None
    cli_unit_tests: int | None = None

    # Bugs + limitations
    open_bugs: list[str] = field(default_factory=list)
    fixed_bugs: list[str] = field(default_factory=list)
    known_limitations: list[str] = field(default_factory=list)

    # Per-feature verdicts (raw strings from the JSON)
    feature_verdicts: dict[str, str] = field(default_factory=dict)

    # Phase-specific signals worth highlighting in the trend tables.
    highlights: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _coerce_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _coerce_int(x: Any) -> int | None:
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def extract_summary(blob: dict[str, Any]) -> PhaseSummary:
    """Pull what's there from a phase JSON; leave everything else None."""
    out = PhaseSummary(
        phase=int(blob.get("phase", 0)),
        date=str(blob.get("date", "")),
        test_focus=str(blob.get("test_focus", "")),
        verdict=str(blob.get("verdict", "")),
    )

    # ---- cost + duration ----
    out.cost_usd = _coerce_float(
        blob.get("approx_cost_usd")
        or blob.get("total_cost_usd_estimate")
        or _live_results_field(blob, "approx_cost_usd")
        or _sum_scenario_costs(blob)
    )
    out.llm_calls = _coerce_int(blob.get("live_calls") or blob.get("total_llm_calls"))
    out.wall_seconds = _coerce_float(
        blob.get("total_wall_seconds")
        or _live_test_field(blob, "duration_seconds")
        or _live_test_field(blob, "duration_seconds_actual")
        or blob.get("duration_seconds_actual")
    )

    # ---- scheduler-style counters (P3, P6, P7) ----
    live = blob.get("live_test") or {}
    results = live.get("results") if isinstance(live, dict) else {}
    if isinstance(results, dict):
        out.dreams_fired = _coerce_int(results.get("dreams_fired"))
        out.skipped_ineligible = _coerce_int(results.get("skipped_ineligible"))
        out.errors = _coerce_int(results.get("errors"))
        out.is_running_at_end = results.get("is_running_at_end")
        out.unique_titles = _coerce_int(results.get("unique_titles"))
        out.avg_seconds_per_dream = _coerce_float(
            results.get("avg_seconds_per_dream_in_cycle")
        )

    # ---- P5 has two schedulers split into part_2_concurrent_dream_schedulers ----
    p5_block = blob.get("part_2_concurrent_dream_schedulers", {}) or {}
    p5_results = p5_block.get("results") if isinstance(p5_block, dict) else {}
    if isinstance(p5_results, dict):
        out.dreams_fired = (
            _coerce_int(p5_results.get("total_dreams_fired")) or out.dreams_fired
        )
        out.errors = (
            _coerce_int(p5_results.get("agent_a", {}).get("errors", 0))
            if isinstance(p5_results.get("agent_a"), dict)
            else out.errors
        )
        out.unique_titles = (
            _coerce_int(p5_results.get("unique_titles_total")) or out.unique_titles
        )

    # ---- P6 has a top-level stats dict ----
    stats = blob.get("stats", {}) or blob.get("live_test", {}).get("stats", {}) or {}
    if isinstance(stats, dict):
        if out.dreams_fired is None:
            out.dreams_fired = _coerce_int(stats.get("dreams_fired"))
        if out.errors is None:
            out.errors = _coerce_int(stats.get("errors"))
        if out.is_running_at_end is None:
            out.is_running_at_end = stats.get("is_running_at_end")

    # ---- P1, P2 scenarios array ----
    dreams_from_scenarios = _sum_dreams_from_scenarios(blob)
    if out.dreams_fired is None and dreams_from_scenarios is not None:
        out.dreams_fired = dreams_from_scenarios

    # ---- P7 final_dreams_total (cumulative across processes) ----
    if out.dreams_fired is None:
        out.dreams_fired = _coerce_int(blob.get("final_dreams_total"))
    live_test = blob.get("live_test") or {}
    if out.dreams_fired is None and isinstance(live_test, dict):
        out.dreams_fired = _coerce_int(live_test.get("final_dreams_total"))

    # ---- unique titles total seen / unique_titles_pct ----
    out.unique_titles_total_seen = _coerce_int(blob.get("unique_titles"))
    if out.unique_titles is None and isinstance(live_test, dict):
        out.unique_titles = _coerce_int(live_test.get("unique_titles_seen"))

    # ---- test counts ----
    tss = blob.get("test_suite_stats") or {}
    if isinstance(tss, dict):
        out.cli_unit_tests = _coerce_int(tss.get("cli_unit_tests"))
        out.dreamscape_unit_tests = _coerce_int(tss.get("dreamscape_specific"))

    # ---- bugs ----
    bugs = blob.get("bugs", {}) or {}
    if isinstance(bugs, dict):
        out.open_bugs = list(bugs.get("open") or [])
        out.fixed_bugs = list(bugs.get("fixed_this_phase") or [])
        out.known_limitations = list(bugs.get("known_limitations") or [])
    # P3+ uses flat fields
    out.open_bugs = list(blob.get("open_bugs", out.open_bugs))
    out.known_limitations = list(blob.get("known_limitations", out.known_limitations))

    if "bug_found_and_fixed" in blob and not out.fixed_bugs:
        bff = blob.get("bug_found_and_fixed")
        if isinstance(bff, dict):
            out.fixed_bugs = [
                str(bff.get("name", "imagination middleware silent failure"))
            ]

    # ---- feature verdicts ----
    out.feature_verdicts = dict(blob.get("feature_verdicts") or {})

    # ---- highlights ----
    out.highlights = _extract_highlights(blob)

    return out


def _sum_scenario_costs(blob: dict[str, Any]) -> float | None:
    scenarios = blob.get("scenarios") or []
    if not scenarios:
        return None
    total = 0.0
    seen = False
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        cost = (s.get("metrics") or {}).get("approx_cost_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
            seen = True
    return total if seen else None


def _sum_dreams_from_scenarios(blob: dict[str, Any]) -> int | None:
    scenarios = blob.get("scenarios") or []
    if not scenarios:
        return None
    total = 0
    seen = False
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        d = (s.get("metrics") or {}).get("dreams_generated")
        if isinstance(d, int):
            total += d
            seen = True
    return total if seen else None


def _live_test_field(blob: dict[str, Any], field: str) -> Any:
    live = blob.get("live_test")
    return live.get(field) if isinstance(live, dict) else None


def _live_results_field(blob: dict[str, Any], field: str) -> Any:
    live = blob.get("live_test") or {}
    if not isinstance(live, dict):
        return None
    results = live.get("results")
    return results.get(field) if isinstance(results, dict) else None


def _extract_highlights(blob: dict[str, Any]) -> list[str]:
    """Pull the most-quotable trend-relevant line from a phase JSON."""
    notes = blob.get("qualitative_notes") or []
    if notes:
        return list(notes[:2])
    live = blob.get("live_test") or {}
    if isinstance(live, dict) and live.get("qualitative_notes"):
        return list(live["qualitative_notes"][:2])
    return []


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


_HEADER = """# Dreamscape — cross-phase trends

> **Auto-generated** from `docs/dreamscape-runs/phase-*.json`. To
> regenerate, run `python scripts/build_dreamscape_trends.py` from the
> repo root. Manual edits to this file will be overwritten — edit the
> per-phase JSON snapshots or the build script instead.

Living document. Updated whenever a new phase snapshot lands. Tracks
whether dreamscape's features are *holding steady*, *improving*, or
*regressing* over time.

See `README.md` for the snapshot schema. Source data: the
`phase-NNN-YYYY-MM-DD.json` files in this directory.

"""


def _cell(value: Any, *, fmt: str = "") -> str:
    if value is None or value == "" or value == []:
        return "n/a"
    if isinstance(value, bool):
        return "✓" if value else "✗"
    if fmt and isinstance(value, (int, float)):
        return format(value, fmt)
    return str(value)


def _phase_header_row(summaries: list[PhaseSummary]) -> str:
    return "| Metric | " + " | ".join(f"P{s.phase}" for s in summaries) + " |\n"


def _phase_divider_row(summaries: list[PhaseSummary]) -> str:
    return "|---|" + "|".join("---" for _ in summaries) + "|\n"


def _render_pass_rate_table(summaries: list[PhaseSummary]) -> str:
    rows: list[tuple[str, list[str]]] = [
        (
            "Dreams fired (live tests)",
            [_cell(s.dreams_fired) for s in summaries],
        ),
        (
            "Scheduler errors",
            [_cell(s.errors) for s in summaries],
        ),
        (
            "Unique titles (in-test)",
            [_cell(s.unique_titles) for s in summaries],
        ),
        (
            "Open bugs (end of phase)",
            [_cell(len(s.open_bugs)) for s in summaries],
        ),
        (
            "Known limitations (carried)",
            [_cell(len(s.known_limitations)) for s in summaries],
        ),
        (
            "Dreamscape unit tests",
            [_cell(s.dreamscape_unit_tests) for s in summaries],
        ),
        (
            "CLI total unit tests",
            [_cell(s.cli_unit_tests) for s in summaries],
        ),
    ]
    out = "## Pass-rate over time\n\n"
    out += _phase_header_row(summaries)
    out += _phase_divider_row(summaries)
    for label, cells in rows:
        out += f"| **{label}** | " + " | ".join(cells) + " |\n"
    out += "\n"
    return out


def _render_performance_table(summaries: list[PhaseSummary]) -> str:
    rows: list[tuple[str, list[str]]] = [
        (
            "LLM calls per phase",
            [_cell(s.llm_calls) for s in summaries],
        ),
        (
            "Total wall-clock (s)",
            [
                _cell(s.wall_seconds, fmt=".1f") if s.wall_seconds else "n/a"
                for s in summaries
            ],
        ),
        (
            "Total cost (USD)",
            [
                _cell(s.cost_usd, fmt=".3f") if s.cost_usd is not None else "n/a"
                for s in summaries
            ],
        ),
        (
            "Avg seconds per dream",
            [
                _cell(s.avg_seconds_per_dream, fmt=".1f")
                if s.avg_seconds_per_dream
                else "n/a"
                for s in summaries
            ],
        ),
    ]
    out = "## Performance over time\n\n"
    out += _phase_header_row(summaries)
    out += _phase_divider_row(summaries)
    for label, cells in rows:
        out += f"| **{label}** | " + " | ".join(cells) + " |\n"
    out += "\n"
    return out


_VERDICT_GLYPH = {
    "sings": "🟢 sings",
    "works": "🟢 works",
    "ironclad": "🟢 ironclad",
    "partial": "🟡 partial",
    "n/a": "n/a",
}


def _glyph_for(verdict: str) -> str:
    if not verdict:
        return "n/a"
    low = verdict.lower()
    if "sing" in low:
        return "🟢 " + verdict
    if "iron" in low:
        return "🟢 " + verdict
    if "work" in low:
        return "🟢 " + verdict
    if "partial" in low or "partly" in low:
        return "🟡 " + verdict
    return verdict


def _render_feature_verdicts(summaries: list[PhaseSummary]) -> str:
    all_features: list[str] = []
    seen: set[str] = set()
    for s in summaries:
        for k in s.feature_verdicts:
            if k not in seen:
                seen.add(k)
                all_features.append(k)
    out = "## Feature verdict history\n\n"
    out += _phase_header_row(summaries).replace("Metric", "Feature")
    out += _phase_divider_row(summaries)
    for feat in all_features:
        cells = []
        for s in summaries:
            v = s.feature_verdicts.get(feat, "")
            cells.append(_glyph_for(v) if v else "n/a")
        # Friendly display name
        label = feat.replace("_", " ").replace(
            "imagination injection", "Imagination injection"
        )
        out += f"| {label} | " + " | ".join(cells) + " |\n"
    out += "\n"
    return out


def _render_cumulative_cost(summaries: list[PhaseSummary]) -> str:
    out = "## Cumulative cost\n\n| Phase | LLM cost (est.) | Cumulative |\n|---|---|---|\n"
    running = 0.0
    for s in summaries:
        cost = s.cost_usd or 0.0
        running += cost
        cost_cell = f"${cost:.3f}" if s.cost_usd is not None else "n/a"
        out += f"| {s.phase} | {cost_cell} | ${running:.3f} |\n"
    out += f"\n**{len(summaries)} phases for under ${(running + 0.005):.2f}.** Cheap data.\n\n"
    return out


def _render_phase_log(summaries: list[PhaseSummary]) -> str:
    out = "## Phase log\n\n"
    for s in summaries:
        title = s.test_focus or "(no focus recorded)"
        # Trim long focus strings to one sentence
        if "." in title:
            title = title.split(".", 1)[0].strip() + "."
        verdict = s.verdict or "n/a"
        out += f"* **Phase {s.phase} — {s.date}.** {title} Verdict: **{verdict}**\n"
    out += "\n"
    return out


def _render_provenance(summaries: list[PhaseSummary]) -> str:
    out = "## Provenance\n\n"
    out += "| Phase | Date | Model | Verdict | Source |\n|---|---|---|---|---|\n"
    for s in summaries:
        src = f"`phase-{s.phase:03d}-{s.date}.json`"
        out += f"| {s.phase} | {s.date} | claude-haiku-4-5 | {s.verdict or 'n/a'} | {src} |\n"
    out += "\n"
    return out


def render_markdown(summaries: list[PhaseSummary]) -> str:
    parts = [
        _HEADER,
        _render_pass_rate_table(summaries),
        _render_performance_table(summaries),
        _render_feature_verdicts(summaries),
        _render_cumulative_cost(summaries),
        _render_phase_log(summaries),
        _render_provenance(summaries),
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# I/O glue
# ---------------------------------------------------------------------------


def load_phase_summaries(phase_dir: Path | None = None) -> list[PhaseSummary]:
    """Load all phase JSONs in numeric order. Returns normalized records."""
    target = phase_dir or PHASE_DIR
    if not target.exists():
        return []
    summaries: list[PhaseSummary] = []
    for path in sorted(target.glob("phase-*.json")):
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARN: failed to read {path}: {exc}", file=sys.stderr)
            continue
        summaries.append(extract_summary(blob))
    summaries.sort(key=lambda s: s.phase)
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if trends.md is out of date (CI-friendly).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=TRENDS_FILE,
        help="Output path (default: docs/dreamscape-runs/trends.md).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PHASE_DIR,
        help="Source directory of phase JSONs.",
    )
    args = parser.parse_args(argv)

    summaries = load_phase_summaries(args.source)
    if not summaries:
        print(f"No phase JSONs found in {args.source}", file=sys.stderr)
        return 2

    new_md = render_markdown(summaries)

    if args.check:
        existing = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if existing == new_md:
            print(f"trends.md is up to date ({len(summaries)} phases).")
            return 0
        print(
            "trends.md is STALE. Run: python scripts/build_dreamscape_trends.py",
            file=sys.stderr,
        )
        return 1

    args.out.write_text(new_md, encoding="utf-8")
    print(
        f"Wrote {args.out} from {len(summaries)} phase JSON(s) "
        f"({sum(s.cost_usd or 0 for s in summaries):.3f} USD total)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — script entrypoint
    raise SystemExit(main())
