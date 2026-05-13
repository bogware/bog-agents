# Dreamscape long-term test data

This directory is the canonical home for **structured snapshots** of every
real-world dreamscape testing pass. Each phase produces two files:

```
phase-NNN-YYYY-MM-DD.json    # machine-readable metrics for trend analysis
phase-NNN-YYYY-MM-DD.md      # human-readable narrative + observations
```

The JSON is the load-bearing artifact — it has a stable schema (see
`schema.md` below) so future phases can be diffed automatically. The
markdown is for humans who want to read the story of one phase.

The cross-phase view lives in **`trends.md`** alongside this README.
It's regenerated whenever a new phase lands; the format keeps a
running table of every metric so you can see how dreamscape's
effectiveness moves over time.

## Why we keep this in the repo

* **Long-term trends require historical data.** A single test pass
  tells you "does it work today"; many passes tell you "is it
  getting better, plateauing, or regressing."
* **Version control = automatic diff.** `git log -p docs/dreamscape-runs/`
  is the timeline. No external DB to maintain.
* **Reproducibility.** Each snapshot records the branch SHA, model
  name, seed when applicable, and full prompt/response excerpts —
  enough to re-run the same test on demand.
* **Memory-keeper.** Future Claude sessions that consult auto-memory
  can find this directory through the memory pointer at
  `~/.claude/projects/.../memory/dreamscape-runs.md`.

## Snapshot schema (v1)

The JSON file structure:

```json
{
  "phase": 1,
  "date": "2026-05-12",
  "branch_sha": "f417af2",
  "model": "claude-haiku-4-5",
  "total_llm_calls": 12,
  "total_wall_seconds": 76,
  "total_cost_usd_estimate": 0.014,
  "bugs": {
    "open": ["laws-hyphen-vs-space", "laws-stop-words", "dashboard-staleness"],
    "fixed_this_phase": []
  },
  "scenarios": [
    {
      "name": "dream-cycle",
      "verdict": "passes",
      "metrics": {
        "dreams_generated": 5,
        "unique_titles_ratio": 1.0,
        "avg_seconds_per_dream": 6.4,
        "final_imagination_trait": 12.5,
        "rng_seed_strategy": "i*7"
      },
      "qualitative_notes": "..."
    },
    ...
  ],
  "verdict": "ship-after-bugfix",
  "next_steps": ["...", "..."]
}
```

Every phase records the same shape so `trends.md` can compute deltas.
Add new metrics in later phases by extending the schema (back-compat is
preserved by treating missing keys as "not measured").

## How to add a new phase

1. Run your test scenarios (typically against `feat/agentic-evolution`
   or the merge target).
2. Capture metrics into `phase-NNN-YYYY-MM-DD.json` using the schema
   above (next phase number, today's ISO date).
3. Write the narrative observations into the matching `.md`.
4. Append a row to `trends.md`.
5. Commit with a `test(cli): phase NNN dreamscape pass` message.

## Phase log

* **Phase 1 (2026-05-12)** — baseline. 5 scenarios, 3 bugs found.
  See `phase-001-2026-05-12.{json,md}` and the longer narrative at
  `docs/DREAMSCAPE_TEST_REPORT.md` (preserved as-is — it's the
  human-rich first pass).
* **Phase 2 (2026-05-13)** — bug-fix verification + re-run. 3 bugs
  closed; new regression tests pinning each. See
  `phase-002-2026-05-13.{json,md}`.
