# Dreamscape — cross-phase trends

Living document. Updated whenever a new phase snapshot lands. The
goal: track whether dreamscape's features are *holding steady*,
*improving*, or *regressing* over time.

See `README.md` for the snapshot schema. Source data: the
`phase-NNN-YYYY-MM-DD.json` files in this directory.

## Pass-rate over time

| Metric | Phase 1 (2026-05-12) | Phase 2 (2026-05-13) | Phase 3 (2026-05-13) | Trend |
|---|---|---|---|---|
| **Laws phrase-match accuracy** | 3/9 (33%) | 9/9 (100%) | 9/9 (100%) | ⬆ +67pp Phase 1→2, held |
| **Dream uniqueness** | 5/5 (single batch) | 5/5 (single batch) | 10/10 (multi-cycle) | → stable, broader window confirms |
| **Cross-agent post coverage** | 3/3 | 3/3 | n/a (not retested) | → stable |
| **Open bugs** | 4 | 0 | 0 | held at 0 |
| **Known limitations** | 0 (uncatalogued) | 1 (stem-matching) | 2 (+scheduler self-restart shape) | catalogued, deferred |
| **Dreamscape unit tests** | 30 | 37 | 42 | ⬆ +5 (scheduler coverage) |
| **CLI total unit tests** | 3522 | 3529 | 3534 | ⬆ +5 |
| **Background scheduler dreams per 90s** | n/a | n/a | **10** | first live multi-cycle pass |
| **Scheduler errors across 11 ticks** | n/a | n/a | **0** | clean |

## Performance over time

| Metric | Phase 1 | Phase 2 | Phase 3 | Trend |
|---|---|---|---|---|
| Avg seconds per dream (Haiku 4.5) | 6.4s | 6.1s | 8.4s in-cycle (~6s call + 2s dreaming-window gap) | → call cost stable; gap is by-design |
| Total wall-clock per phase | 76s | 50s | 90s | scheduler-bounded, not call-bounded |
| Total cost per phase | $0.014 | $0.010 | $0.012 | → flat |
| LLM calls per phase | 12 | 8 | 10 | scheduler fired 10 cycles cleanly |

Per-dream-cost remains the steady-state baseline; Phase 3 confirms it
holds when the scheduler is in the loop (i.e. no extra overhead from
the background timer itself).

## Feature verdict history

| Feature | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Dream engine | 🟢 sings | 🟢 sings | 🟢 sings |
| **Dream scheduler** | n/a | n/a | 🟢 **sings — multi-cycle validated** |
| Imagination injection | 🟢 sings | 🟢 sings | 🟢 sings (compounds correctly 0→15.0) |
| Cross-agent shared memory | 🟢 sings | 🟢 sings | 🟢 sings (no re-run, mechanism unchanged) |
| Lifecycle state machine | 🟢 works | 🟢 works | 🟢 works (10 dormant→dreaming→dormant cycles, zero leakage) |
| Laws hard rejects | 🟡 partial | 🟢 works | 🟢 works |
| Constitution soft logging | 🟢 works | 🟢 works | 🟢 works |
| Agent-state dashboard | 🟡 staleness bug | 🟢 works | 🟢 works |
| Repo overview | 🟢 works | 🟢 works | 🟢 works |
| Opt-in defaults | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad |

## Cumulative cost

| Phase | LLM cost (est.) | Cumulative |
|---|---|---|
| 1 | $0.014 | $0.014 |
| 2 | $0.010 | $0.024 |
| 3 | $0.012 | $0.036 |

Three phases for under four pennies. Cheap data.

## What this trend view tells us

Three data points and the picture sharpens:

1. **The bug fixes from Phase 2 stuck.** Laws pass-rate held at 100%
   into Phase 3. The normaliser work continues to behave.
2. **Per-call cost is genuinely stable.** Phase 3 introduced a new
   delivery mechanism (background timer) but the per-dream LLM cost
   sits at the same ~$0.001/call as Phases 1 and 2 — the scheduler
   itself adds no measurable overhead.
3. **The orphaned-callable problem is closed.** `maybe_dream` is no
   longer dead code; an asyncio task now drives it on a poll, and 10
   consecutive cycles fired cleanly with zero errors and zero state
   leakage between dormant/dreaming transitions.
4. **Variety holds across cycles, not just batches.** Phase 1/2 dream
   uniqueness was measured within a single 5-pass batch. Phase 3
   extended that to 10 cycles over 90 seconds and still got 10/10
   unique titles — the RNG seeding is not just picking unique seeds
   *per batch*, it's picking unique seeds *per cycle*.

The next real signal will come at **Phase 4**, when we drop the
accelerated knobs and run a real multi-day cycle with production
timing (poll=60s, dormancy=1800s, dreaming=600s). That tests whether
the asyncio task survives a long quiet stretch — i.e. whether the
timer holds across overnight runs, not just whether it fires in a
tight loop.

## Phase log

* **Phase 1 — 2026-05-12 (baseline).** 5 scenarios, 12 LLM calls,
  3 bugs found. Verdict: ship-after-bugfix.
* **Phase 2 — 2026-05-13 (bug-fix verification).** 6 scenarios
  (re-ran 1, 4, 5; added bug-fix verification for 3 + dashboard).
  All Phase-1 bugs fixed. 8 regression tests added.
  Verdict: **READY TO MERGE.**
* **Phase 3 — 2026-05-13 (multi-cycle scheduler validation).** Built
  `DreamScheduler` (250 lines, 5 unit tests) and wired it into
  `LifecycleMiddleware` via a lazy-start factory. Live test: 90s of
  accelerated-time scheduling with real Haiku 4.5 fired **10 dreams,
  10/10 unique titles, 0 errors** across 11 ticks. Imagination
  compounded 0 → 15.0 exactly as designed. Verdict: **READY TO MERGE.**
