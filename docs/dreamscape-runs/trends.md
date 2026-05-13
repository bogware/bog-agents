# Dreamscape — cross-phase trends

Living document. Updated whenever a new phase snapshot lands. The
goal: track whether dreamscape's features are *holding steady*,
*improving*, or *regressing* over time.

See `README.md` for the snapshot schema. Source data: the
`phase-NNN-YYYY-MM-DD.json` files in this directory.

## Pass-rate over time

| Metric | P1 (05-12) | P2 (05-13) | P3 (05-13) | P4 (05-13) | P5 (05-13) | Trend |
|---|---|---|---|---|---|---|
| **Laws phrase-match accuracy** | 3/9 (33%) | 9/9 (100%) | 9/9 | 9/9 | 9/9 | held at 100% since P2 |
| **Dream uniqueness** | 5/5 | 5/5 | 10/10 (multi-cycle) | n/a (mechanism phase) | **15/15** (multi-agent) | broader windows keep confirming |
| **Cross-agent shared-memory writes** | 3/3 | 3/3 | n/a | n/a | **50/50** (p95 <10ms under WAL) | concurrent-safe |
| **Open bugs** | 4 | 0 | 0 | **1 found + fixed** (imagination silently broken) | 0 | net zero, +1 root-cause found |
| **Known limitations** | 0 | 1 (stem-matching) | 2 (+scheduler recursion) | 2 | 2 | catalogued, deferred |
| **Dreamscape unit tests** | 30 | 37 | 42 | **43** | 43 | ⬆ +1 (imagination regression test) |
| **CLI total unit tests** | 3522 | 3529 | 3534 | **3535** | 3535 | ⬆ +1 |
| **Background scheduler dreams (per phase)** | n/a | n/a | 10 (90s, 1 agent) | n/a | **15** (60s, 2 agents) | concurrency adds zero errors |
| **Scheduler errors** | n/a | n/a | 0 | n/a | 0 | clean |

## Performance over time

| Metric | P1 | P2 | P3 | P4 | P5 | Trend |
|---|---|---|---|---|---|---|
| Avg seconds per dream call (Haiku 4.5) | 6.4s | 6.1s | ~6s + 2s window | n/a | ~7s/cycle/agent | call cost flat |
| Total wall-clock per phase | 76s | 50s | 90s | ~5s | ~61s | scheduler-bounded |
| Total cost per phase | $0.014 | $0.010 | $0.012 | $0.002 | $0.015 | flat per-call |
| LLM calls per phase | 12 | 8 | 10 | 2 | 15 | matches workload shape |
| Concurrent SQLite write p95 latency | n/a | n/a | n/a | n/a | **<10 ms** (50 writes) | WAL handles it |

## Feature verdict history

| Feature | P1 | P2 | P3 | P4 | P5 |
|---|---|---|---|---|---|
| Dream engine | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 | 🟢 |
| Dream scheduler | n/a | n/a | 🟢 sings | 🟢 | 🟢 **multi-agent-safe** |
| Imagination injection | 🟢 sings* | 🟢 sings* | 🟢 sings* | 🟢 **sings (actually now)** | 🟢 |
| Cross-agent shared memory | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 | 🟢 **concurrent-write verified** |
| Lifecycle state machine | 🟢 works | 🟢 works | 🟢 works | 🟢 | 🟢 multi-agent verified |
| Laws hard rejects | 🟡 partial | 🟢 works | 🟢 | 🟢 | 🟢 |
| Constitution soft logging | 🟢 works | 🟢 works | 🟢 | 🟢 | 🟢 |
| Agent-state dashboard | 🟡 staleness | 🟢 works | 🟢 | 🟢 | 🟢 |
| Repo overview | 🟢 works | 🟢 works | 🟢 | 🟢 | 🟢 |
| Opt-in defaults | 🟢 ironclad | 🟢 ironclad | 🟢 | 🟢 | 🟢 |

*P1-P3 marked imagination injection 🟢 by mechanism (gating logic). P4
found that the execution path was silently broken (passing the wrong
type to the SDK's `append_to_system_message` helper). Three prior
verdicts missed it because no test drove the full injection flow.
P4's verdict 🟢 is the first time imagination has actually been
end-to-end-verified.

## Cumulative cost

| Phase | LLM cost (est.) | Cumulative |
|---|---|---|
| 1 | $0.014 | $0.014 |
| 2 | $0.010 | $0.024 |
| 3 | $0.012 | $0.036 |
| 4 | $0.002 | $0.038 |
| 5 | $0.015 | $0.053 |

Five phases for under six pennies. Cheap data.

## What this trend view tells us

Five data points and a few things crystallize:

1. **Phase 4 is the most important phase so far.** Phases 1-3 all
   marked imagination injection green by *mechanism* — `_should_inject`
   returned True at the right times. Phase 4 drove the actual
   injection path and found the SDK helper was being called with the
   wrong type, raising silently inside the broad `except Exception`
   that exists to "never raise into the prompt path." That same
   defensive try/except is what hid the bug for three phases. The
   takeaway: *defensive error swallowing requires aggressive
   end-to-end testing to compensate.*
2. **Multi-agent concurrency is a non-event.** Phase 5 ran two
   schedulers and two writers in parallel against the same SQLite
   store and got zero errors, zero collisions, p95 < 10ms on
   contended writes. WAL mode + per-agent IDs are doing their job.
3. **Per-dream cost is stable across every phase.** P1=$0.0012/dream,
   P2=$0.0013/dream, P3=$0.0012/dream, P5=$0.001/dream. The
   scheduler and the concurrency add no measurable overhead.
4. **Title diversity scales.** P1/P2 measured 5-of-5 within a single
   batch. P3 saw 10-of-10 across a 90s multi-cycle run. P5 saw
   15-of-15 across two agents running concurrently. The seed library
   plus RNG sampling is robust under both temporal and spatial
   pressure.
5. **Production cadence is now well-characterized.** At
   `imagination_trait_increment=0.01`, an agent gains ~0.08 trait per
   minute of continuous dreaming. The default
   `min_imagination_trait=1.0` threshold for injection therefore
   takes ~12.5 hours of dreaming to unlock — exactly the "you have
   to earn it" design intent.

The next real signal: **Phase 6 (suggested)** drops the accelerated
knobs and runs production timing (poll=60s, dormancy=1800s,
dreaming=600s) overnight to verify the asyncio task survives long
quiet stretches. That's the gap that no live phase has tested yet.

## Phase log

* **Phase 1 — 2026-05-12 (baseline).** 5 scenarios, 12 LLM calls,
  3 bugs found. Verdict: ship-after-bugfix.
* **Phase 2 — 2026-05-13 (bug-fix verification).** 6 scenarios. All
  Phase-1 bugs fixed. 8 regression tests added. Verdict: ready-to-merge.
* **Phase 3 — 2026-05-13 (multi-cycle scheduler validation).** Built
  `DreamScheduler` (250 lines, 5 unit tests). Live test: 90s
  accelerated cycle, 10 unique dreams, 0 errors. Verdict: ready-to-merge.
* **Phase 4 — 2026-05-13 (imagination injection live test).** Found
  + fixed a silent-failure bug in `_maybe_inject` (wrong type passed
  to `append_to_system_message`). Verified live A/B against Haiku
  4.5: treatment response framing diverged from control without
  leaking dream vocabulary. 1 regression test added.
  Verdict: **READY TO MERGE — bug fix is load-bearing.**
* **Phase 5 — 2026-05-13 (concurrent multi-agent).** Two schedulers +
  50 interleaved SQLite writes from two agents in parallel. 15 unique
  dreams, 0 errors, p95 <10ms on shared-memory writes. Concurrency
  is a non-event. Verdict: **READY TO MERGE.**
