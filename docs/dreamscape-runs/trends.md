# Dreamscape — cross-phase trends

Living document. Updated whenever a new phase snapshot lands. The
goal: track whether dreamscape's features are *holding steady*,
*improving*, or *regressing* over time.

See `README.md` for the snapshot schema. Source data: the
`phase-NNN-YYYY-MM-DD.json` files in this directory.

## Pass-rate over time

| Metric | Phase 1 (2026-05-12) | Phase 2 (2026-05-13) | Trend |
|---|---|---|---|
| **Laws phrase-match accuracy** | 3/9 (33%) | 9/9 (100%) | ⬆ +67pp (bug fixes) |
| **Dream uniqueness (5-pass)** | 5/5 | 5/5 | → stable |
| **Cross-agent post coverage** | 3/3 | 3/3 | → stable |
| **Open bugs** | 4 | 0 | ⬇ -4 (all fixed) |
| **Known limitations** | 0 (uncatalogued) | 1 (stem-matching) | new entry, deferred |
| **Dreamscape unit tests** | 30 | 37 | ⬆ +7 (regression coverage) |
| **CLI total unit tests** | 3522 | 3529 | ⬆ +7 |

## Performance over time

| Metric | Phase 1 | Phase 2 | Trend |
|---|---|---|---|
| Avg seconds per dream (Haiku 4.5) | 6.4s | 6.1s | → ~stable (-5%) |
| Total wall-clock per phase | 76s | 50s | ⬇ -34% (Phase 2 trimmed redundant calls) |
| Total cost per phase | $0.014 | $0.010 | ⬇ -29% |
| LLM calls per phase | 12 | 8 | ⬇ -33% |

Performance numbers are largely a function of how many scenarios are
re-run rather than dreamscape itself getting faster. The
per-dream-cost number is the steady-state baseline — track that
across phases to spot real regressions.

## Feature verdict history

| Feature | Phase 1 | Phase 2 |
|---|---|---|
| Dream engine | 🟢 sings | 🟢 sings |
| Imagination injection | 🟢 sings | 🟢 sings (no re-run, mechanism unchanged) |
| Cross-agent shared memory | 🟢 sings | 🟢 sings |
| Lifecycle state machine | 🟢 works | 🟢 works |
| Laws hard rejects | 🟡 partial | 🟢 works |
| Constitution soft logging | 🟢 works | 🟢 works |
| Agent-state dashboard | 🟡 staleness bug | 🟢 works |
| Repo overview | 🟢 works | 🟢 works |
| Opt-in defaults | 🟢 ironclad | 🟢 ironclad |

## Cumulative cost

| Phase | LLM cost (est.) | Cumulative |
|---|---|---|
| 1 | $0.014 | $0.014 |
| 2 | $0.010 | $0.024 |

Two phases for less than three pennies. Cheap data.

## What this trend view tells us

Two data points isn't enough to spot drift, but it IS enough to
confirm:

1. **The bug fixes worked.** No regression in pass-rate after the
   normaliser changes; the false-positive rate stayed at zero.
2. **Performance is stable.** Dream generation cost is the same
   per-call; the only meaningful per-phase variation is "how many
   scenarios did the tester re-run."
3. **The features that scored 🟢 in Phase 1 still score 🟢.** The
   yellow flags from Phase 1 (laws partial, dashboard staleness)
   both flipped to green in Phase 2.

The first real signal from this trend view will come at **Phase 3**,
when we live-test the multi-day daemon dream cycle — a workload that
exercises the dormancy timer + dream-engine + imagination-injection
under a real schedule rather than synthesised state. That's the
moment we'll see whether the in-memory state correctly hands off
to long-running background processes.

## Phase log

* **Phase 1 — 2026-05-12 (baseline).** 5 scenarios, 12 LLM calls,
  3 bugs found. Verdict: ship-after-bugfix.
* **Phase 2 — 2026-05-13 (bug-fix verification).** 6 scenarios
  (re-ran 1, 4, 5; added bug-fix verification for 3 + dashboard).
  All Phase-1 bugs fixed. 8 regression tests added.
  Verdict: **READY TO MERGE.**
