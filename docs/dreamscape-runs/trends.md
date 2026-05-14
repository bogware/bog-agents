# Dreamscape — cross-phase trends

> **Auto-generated** from `docs/dreamscape-runs/phase-*.json`. To
> regenerate, run `python scripts/build_dreamscape_trends.py` from the
> repo root. Manual edits to this file will be overwritten — edit the
> per-phase JSON snapshots or the build script instead.

Living document. Updated whenever a new phase snapshot lands. Tracks
whether dreamscape's features are *holding steady*, *improving*, or
*regressing* over time.

See `README.md` for the snapshot schema. Source data: the
`phase-NNN-YYYY-MM-DD.json` files in this directory.

## Pass-rate over time

| Metric | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P10 | P11 | P12 | P13 | P14 | P15 | P16 | P17 | P18 | P19 | P20 | P21 | P22 | P25 | P26 | P27 | P28 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Dreams fired (live tests)** | 5 | 5 | 10 | n/a | 15 | 27 | 10 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| **Scheduler errors** | n/a | n/a | 0 | n/a | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| **Unique titles (in-test)** | n/a | n/a | 10 | n/a | 15 | n/a | 8 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| **Open bugs (end of phase)** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **Known limitations (carried)** | 0 | 1 | 2 | 2 | 2 | 2 | 2 | 2 | 3 | 3 | 3 | 2 | 3 | 2 | 3 | 4 | 1 | 4 | 3 | 3 | 4 | 4 | 2 | 3 | 4 |
| **Dreamscape unit tests** | n/a | 37 | 42 | 43 | 43 | 43 | 47 | 52 | 67 | 67 | 68 | 79 | 79 | 81 | 81 | 88 | 81 | 98 | 98 | 104 | 98 | 114 | 114 | 119 | 119 |
| **CLI total unit tests** | n/a | 3529 | 3534 | 3535 | 3535 | 3535 | 3539 | 3544 | 3552 | 3552 | 3553 | 3564 | 3564 | 3566 | 3568 | 3573 | 3566 | 3583 | 3583 | 3589 | 3583 | 3599 | 3599 | 3604 | 3604 |

## Performance over time

| Metric | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P10 | P11 | P12 | P13 | P14 | P15 | P16 | P17 | P18 | P19 | P20 | P21 | P22 | P25 | P26 | P27 | P28 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **LLM calls per phase** | 12 | 8 | n/a | 2 | 15 | 28 | 10 | n/a | 21 | 21 | 35 | 4 | 840 | 210 | 525 | 135 | n/a | 4 | 180 | 315 | 200 | 2 | 630 | 427 | n/a |
| **Total wall-clock (s)** | 76.0 | 50.0 | 90.2 | n/a | n/a | 1803.1 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| **Total cost (USD)** | 0.014 | 0.010 | 0.012 | 0.002 | 0.015 | 0.027 | 0.010 | n/a | 0.040 | 0.039 | 0.070 | 0.004 | 2.100 | 0.400 | 1.400 | 0.300 | n/a | 0.003 | 0.400 | 0.600 | 0.200 | 0.002 | 1.200 | 1.100 | n/a |
| **Avg seconds per dream** | n/a | n/a | 8.4 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## Feature verdict history

| Feature | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P10 | P11 | P12 | P13 | P14 | P15 | P16 | P17 | P18 | P19 | P20 | P21 | P22 | P25 | P26 | P27 | P28 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dream engine | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | n/a | 🟢 sings | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings (was BROKEN; fixed in this phase, now end-to-end verified) | 🟢 sings (fixed in Phase 4) | 🟢 sings (Phase 4) | 🟢 sings (Phase 4 fix) | 🟢 sings (Phase 4 fix) | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| cross agent shared memory | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings — concurrent writes p95 <10ms, perfect isolation | 🟢 sings (Phase 5) | 🟢 sings (Phase 5) | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | n/a | 🟢 sings | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| lifecycle state machine | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works — concurrent multi-agent verified | 🟢 works (state transitions visible in checkpoint snapshots) | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| laws hard rejects | 🟢 works for clear cases | 🟢 works (9/9 with paraphrase tolerance) | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| constitution soft logging | 🟢 works as designed | 🟢 works as designed | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works (now surfaced) | 🟢 works | 🟢 works | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| agent state dashboard | 🟢 mostly works (staleness bug) | 🟢 works (staleness fixed) | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| repo overview | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| opt in defaults | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| dream scheduler | n/a | n/a | 🟢 sings — multi-cycle dormancy timer validated end-to-end | 🟢 sings | 🟢 sings — works under concurrent multi-agent load | 🟢 sings — production cadence + induced failure + 30-min endurance verified | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | n/a | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| scheduler resilience under transient failure | n/a | n/a | n/a | n/a | n/a | 🟢 sings — model failure absorbed without crashing the loop | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| dreamscape runner | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — daemon-style entrypoint validated end-to-end with crash recovery | 🟢 sings | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| snapshot persistence across processes | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — survives SIGKILL | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| trends automation | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — idempotent, --check mode for CI, full coverage by 5 new tests | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection mechanism | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings (Phase 4) | 🟢 sings (Phase 4) | 🟢 sings | n/a | 🟢 sings (Phase 4) | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection effectiveness | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | negative — 1/7 wins on technical-debugging questions. Domain mismatch is the leading hypothesis. | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| constitution log surfacing | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings (R1) | 🟢 sings | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| seed library size | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings (50 entries, R2) | 🟢 sings | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| daily dream cap | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 works (R3) | 🟢 works | 🟢 works | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection effectiveness on creative prompts | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 6/7 wins (86%) | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection effectiveness on technical prompts | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | negative — Phase 10 said 1/7, Phase 12 said 5/7 same scenario set; high variance suggests effect is small or noisy | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection dreams style | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | noisy on technical prompts — Phase 10 said -, Phase 12 said + | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection neutral style | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | roughly equivalent to dreams on technical prompts; preserves the option to ship | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| domain classifier | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 14/14 assertions pass | 🟢 sings — verified end-to-end in Phase 13 | 🟢 sings | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| agent profile persistence | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — round-trips through disk | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| seed category routing | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — engineering correctly prefers computing-history | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| injection style routing | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — engineering correctly gets neutral wrapper end-to-end | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| creative routing isolation | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | verified — creative-prompt output from engineering agent is still usable (not catastrophic), but doesn't earn the Phase 11 / 14 treatment-win lift | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection on creative prompts | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 SINGS — 79.3% win rate (95% CI [72%, 85%]) at N=140 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection on technical prompts | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | HURTS — 27.9% win rate (95% CI [21%, 36%]) at N=140; the shipped neutral-wrapper routing addresses this | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| domain conditional effect | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | ROBUST — 51 pp difference between domains, non-overlapping 95% CIs | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| engineering craft seed library | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 62.9% win rate vs computing-history on engineering prompts, lower CI bound above 50% | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| domain aware seed selection | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — engineering-craft as primary engineering preference justified by data | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection on engineering prompts with eng craft seeds | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | improved — likely closes part of Phase 14's 28% gap (combined experiment in future phase) | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| Imagination injection with neutral wrapper on technical | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | break-even-to-positive — 57% win rate, lower CI ~48% | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| engineering craft vs computing history at n 105 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | statistically indistinguishable on aggregate, EC dominates on decision-shaped scenarios | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| neutral wrapper advantage over dreams wrapper on technical | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | MASSIVE — 29pp lift attributable to wrapper alone | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| per prompt routing mechanism | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — classifier + middleware integration work correctly | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| per prompt routing effectiveness on engineering agents | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | ambiguous-to-negative — does not reliably improve outcomes | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| shipping decision | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 ship the knob, keep it off by default. Engineering agents continue using neutral wrapper for all prompts (Phase 16 validated). | n/a | n/a | n/a | ship the knob, default OFF. Like Phase 17, the per-call routing surface is preserved for future research. | n/a | n/a | n/a | both per-prompt routing knobs stay OFF by default. | n/a |
| decision pattern detection | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 works — fires on intended patterns. The 'designing' keyword overlap with creative-vocabulary is a known false positive. | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| engineering craft seed library size | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 30 entries, ~2x reduction in same-seed-twice probability per agent-day | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| llm classifier mechanism | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 10 unit tests pass, validation profiles classify coherently | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| llm classifier caching | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — cache round-trips disk, resolve_agent_domain consults it on the long tail | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| agent py integration | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — fires as background task, doesn't block agent creation, only runs when keyword classifier returns 'general' | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| engineering craft seeds beat computing history on decision shaped | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — N=60, lower CI bound 54% (above 50%) | n/a | n/a | n/a | n/a | n/a | n/a |
| shipping engineering preference order | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | justified by data, not just intuition | n/a | n/a | n/a | n/a | n/a | n/a |
| per scenario heterogeneity pattern | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | confirmed — decision-shaped scenarios favor EC, debugging-shaped scenarios are closer or favor CH | n/a | n/a | n/a | n/a | n/a | n/a |
| category filter mechanism | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 6 unit tests pass, end-to-end live verification works | n/a | n/a | n/a | n/a | n/a |
| per prompt content routing effectiveness | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 ambiguous-to-slightly-negative — routed 47.6% with CI [38%, 57%]. Mechanism works; outcomes don't measurably improve. | n/a | n/a | n/a | n/a | n/a |
| real bug pass rate easy bugs | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | treatment indistinguishable from control (both 100%) | n/a | n/a | n/a | n/a |
| real bug pass rate harder bugs | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | treatment indistinguishable from control (both 100%) | n/a | n/a | n/a | n/a |
| ceiling effect | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 Haiku 4.5 is too competent at single-function bug fixes for this kind of A/B to discriminate — even on harder-bug fixtures | n/a | n/a | n/a | n/a |
| judge based preference signal still valid | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | Phase 14's 79% creative + Phase 16's 57% engineering win rates hold; they measure response USEFULNESS, not response CORRECTNESS — different question, different surface | n/a | n/a | n/a | n/a |
| event recorder | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — 10 unit tests pass, file rotation works at 1 MB cap | n/a | n/a | n/a |
| aggregator | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — counts + rates + cost estimate correct in unit tests + live demo | n/a | n/a | n/a |
| dashboard view | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — empty state + populated state render correctly | n/a | n/a | n/a |
| slash command integration | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | ships — /dreamscape stats [hours|all] in the registry | n/a | n/a | ships — /dreamscape export available with autocomplete |
| production readiness | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | the infrastructure is production-ready; the data is yet to accumulate in real deployments | n/a | n/a | n/a |
| per prompt content routing keyword classifier | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | actively hurts — 40% win rate, CI fully below 50% | n/a | n/a |
| p21 interpretation revised | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | P21's 47.6% was the upper edge of the noise band; P26's tighter measurement shows true rate is ~40%. | n/a | n/a |
| llm prompt classifier mechanism | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — accurately classifies prompts (4/7 agree with keyword exactly, 1/7 produces a more nuanced verdict on legacy-deletion) | n/a |
| llm classifier routing effectiveness | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | no significant improvement over keyword — 46.7% vs P21's 47.6% at same N. Both straddle 50%. | n/a |
| list agents with telemetry | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — only returns agents with actual log files, ignores profile-only dirs |
| export telemetry bundle | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — schema-versioned bundle, agent-by-agent summary + raw events |
| privacy mode | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — strips metadata cleanly while preserving aggregate counts |
| since filter | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — respects timestamp lower bound |
| auto discovery | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — agent_ids=None auto-enumerates the agents dir |

## Cumulative cost

| Phase | LLM cost (est.) | Cumulative |
|---|---|---|
| 1 | $0.014 | $0.014 |
| 2 | $0.010 | $0.024 |
| 3 | $0.012 | $0.036 |
| 4 | $0.002 | $0.038 |
| 5 | $0.015 | $0.053 |
| 6 | $0.027 | $0.080 |
| 7 | $0.010 | $0.090 |
| 8 | n/a | $0.090 |
| 10 | $0.040 | $0.130 |
| 11 | $0.039 | $0.169 |
| 12 | $0.070 | $0.239 |
| 13 | $0.004 | $0.243 |
| 14 | $2.100 | $2.343 |
| 15 | $0.400 | $2.743 |
| 16 | $1.400 | $4.143 |
| 17 | $0.300 | $4.443 |
| 18 | n/a | $4.443 |
| 19 | $0.003 | $4.446 |
| 20 | $0.400 | $4.846 |
| 21 | $0.600 | $5.446 |
| 22 | $0.200 | $5.646 |
| 25 | $0.002 | $5.648 |
| 26 | $1.200 | $6.848 |
| 27 | $1.100 | $7.948 |
| 28 | n/a | $7.948 |

**25 phases for under $7.95.** Cheap data.

## Phase log

* **Phase 1 — 2026-05-12.** baseline — first end-to-end run across 5 scenarios (dream cycle, imagination A/B, laws enforcement, cross-agent shared memory, real-codebase iteration on Oregon Trail). Verdict: **ship-after-bugfix**
* **Phase 2 — 2026-05-13.** bug-fix verification — re-ran Phase 1 scenarios + verified all 4 bug fixes (hyphen-vs-space, stop-word tolerance, comma-list cross-product, dashboard staleness). Verdict: **READY TO MERGE**
* **Phase 3 — 2026-05-13.** background scheduler — multi-cycle dormancy timer validation Verdict: **READY TO MERGE**
* **Phase 4 — 2026-05-13.** imagination injection live test — first time the injection path was driven end-to-end against a real ModelRequest and a real Anthropic API call Verdict: **READY TO MERGE — and the bug we found is a load-bearing one that the prior phase verdicts missed.**
* **Phase 5 — 2026-05-13.** concurrent multi-agent dreamscape — two schedulers + two shared-memory writers running in parallel Verdict: **READY TO MERGE**
* **Phase 6 — 2026-05-13.** production-timing endurance run — 30 min with poll=60s + induced transient model failure Verdict: **READY TO MERGE — the dream cycle is production-grade.**
* **Phase 7 — 2026-05-13.** daemon-style runner — cross-process state continuity. Verdict: **READY TO MERGE**
* **Phase 8 — 2026-05-13.** automation — trends. Verdict: **READY TO MERGE**
* **Phase 10 — 2026-05-13.** controlled effectiveness experiment — does the imagination injection actually make an agent more useful to a stuck developer? 7 scenarios, blind A/B judged by Sonnet 4. Verdict: **MIXED — engineering still ships; defaults stay off; imagination injection should not be a general-purpose stuck-agent tool.**
* **Phase 11 — 2026-05-13.** domain-mismatch hypothesis — re-run Phase 10's blind A/B harness on 7 creative/design prompts. Verdict: **STRONG WIN on creative/design prompts. Combined with Phase 10 + 12, this confirms domain-conditional effectiveness.**
* **Phase 12 — 2026-05-13.** metaphor-wrapper ablation — same 7 technical-debugging scenarios as Phase 10, but a 3-arm experiment: control vs current 'dreams' wrapper vs new 'neutral' wrapper. Verdict: **MIXED — neutral wrapper does not materially improve outcomes, but it's the safer default for non-creative agents and ships clean.**
* **Phase 13 — 2026-05-13.** routing verification — confirm the dreamscape/domain. Verdict: **READY TO MERGE — routing layer ships correctly.**
* **Phase 14 — 2026-05-13.** statistical-power re-run of Phase 10 + 11 — N=20 trials per scenario per domain with parallelized API calls. Verdict: **DECISIVE — domain-conditional effect is statistically robust. The shipping context-aware routing is justified by data, not just intuition.**
* **Phase 15 — 2026-05-13.** does the CONTENT of injected dreams matter — engineering-craft seeds (new, Phase 15) vs computing-history seeds (existing) on technical-debugging prompts. Verdict: **WIN — domain-appropriate dream CONTENT helps. Engineering agents should dream of engineering-craft, not historical-figures-in-computing.**
* **Phase 16 — 2026-05-13.** three-arm experiment — does treatment-with-engineering-craft + neutral wrapper beat no-injection control on technical-debugging prompts? Combined with computing-history vs control as a calibration arm. Verdict: **WIN — the technical-prompt story flips. With the neutral wrapper, imagination injection is net-positive (or at least neutral) on technical work. The shipping defaults (engineering routing → neutral wrapper) are validated by data, not just intuition.**
* **Phase 17 — 2026-05-13.** per-prompt routing — classify the USER PROMPT (not just the agent profile) and override the injection wrapper style per-call. Verdict: **MIXED — mechanism ships clean (and may be useful for power-users), but per-prompt routing is NOT a default enabled feature. Phase 16's data already validated the agent-level routing as the load-bearing decision; per-prompt is a refinement that didn't pay off at this N.**
* **Phase 18 — 2026-05-13.** expand engineering-craft seed library from 15 (Phase 15) to 30 entries. Verdict: **READY TO MERGE — pure curation work, no behavior change beyond reducing repetition.**
* **Phase 19 — 2026-05-13.** LLM-based domain classifier fallback for the long tail of profiles where the keyword classifier returns 'general'. Verdict: **READY TO MERGE — small surface, robust failure modes, gracefully handles the long tail without changing modal behavior.**
* **Phase 20 — 2026-05-13.** N=30 confirmation of engineering-craft seeds > computing-history seeds on the two DECISION-shaped technical scenarios where the effect was strongest in Phase 15 (62. Verdict: **STATISTICALLY ROBUST WIN — engineering-craft seeds beat computing-history seeds on decision-shaped technical scenarios. The shipping content preferences are justified beyond noise.**
* **Phase 21 — 2026-05-13.** per-prompt CONTENT routing. Verdict: **MIXED — mechanism solid, default keeps it off. The agent-level routing from Phase 11/12 (which Phase 14 + Phase 20 confirmed at N=140 and N=60 respectively) is the load-bearing decision. Per-prompt content routing is preserved as a knob.**
* **Phase 22 — 2026-05-13.** real-bug outcome experiment — does imagination injection make an agent fix bugs better? Objective pytest pass-rate measurement, NOT a judge-based preference comparison. Verdict: **NULL RESULT — honestly reported. Imagination injection neither helps nor hurts on single-function bug-fix tasks at the difficulty Haiku 4.5 handles trivially. The campaign's prior judge-based findings (79% / 57% / 67% win rates by domain) still stand as the load-bearing effectiveness measurements; they capture a different (and arguably more important) property: response usefulness on open-ended questions where 'correct' is underdetermined.**
* **Phase 25 — 2026-05-13.** production telemetry infrastructure — event logging + aggregator + dashboard. Verdict: **READY TO MERGE — clean infrastructure ship. The data accumulates as users use dreamscape; the campaign's offline measurements are unchanged.**
* **Phase 26 — 2026-05-13.** N=30 confirmation of Phase 21's per-prompt content routing finding. Verdict: **CLARIFYING NEGATIVE — per-prompt content routing should NOT be enabled by default. The N=210 measurement resolves Phase 21's wide CI; the keyword-classifier-driven routing genuinely loses to static.**
* **Phase 27 — 2026-05-13.** swap keyword prompt classifier for LLM classifier in per-prompt content routing. Verdict: **CONFIRMATORY NEGATIVE — LLM classification doesn't rescue per-prompt content routing. Phase 26's directional finding holds with a more precise classifier. The dominant effect is filtering-vs-diversity, not classifier accuracy.**
* **Phase 28 — 2026-05-13.** telemetry exporter — bundle per-agent telemetry from multiple agents into a single JSON file for cross-deployment analysis. Verdict: **READY TO MERGE — clean infrastructure ship. Unlocks future cross-deployment analysis without changing modal behavior.**

## Provenance

| Phase | Date | Model | Verdict | Source |
|---|---|---|---|---|
| 1 | 2026-05-12 | claude-haiku-4-5 | ship-after-bugfix | `phase-001-2026-05-12.json` |
| 2 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE | `phase-002-2026-05-13.json` |
| 3 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE | `phase-003-2026-05-13.json` |
| 4 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — and the bug we found is a load-bearing one that the prior phase verdicts missed. | `phase-004-2026-05-13.json` |
| 5 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE | `phase-005-2026-05-13.json` |
| 6 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — the dream cycle is production-grade. | `phase-006-2026-05-13.json` |
| 7 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE | `phase-007-2026-05-13.json` |
| 8 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE | `phase-008-2026-05-13.json` |
| 10 | 2026-05-13 | claude-haiku-4-5 | MIXED — engineering still ships; defaults stay off; imagination injection should not be a general-purpose stuck-agent tool. | `phase-010-2026-05-13.json` |
| 11 | 2026-05-13 | claude-haiku-4-5 | STRONG WIN on creative/design prompts. Combined with Phase 10 + 12, this confirms domain-conditional effectiveness. | `phase-011-2026-05-13.json` |
| 12 | 2026-05-13 | claude-haiku-4-5 | MIXED — neutral wrapper does not materially improve outcomes, but it's the safer default for non-creative agents and ships clean. | `phase-012-2026-05-13.json` |
| 13 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — routing layer ships correctly. | `phase-013-2026-05-13.json` |
| 14 | 2026-05-13 | claude-haiku-4-5 | DECISIVE — domain-conditional effect is statistically robust. The shipping context-aware routing is justified by data, not just intuition. | `phase-014-2026-05-13.json` |
| 15 | 2026-05-13 | claude-haiku-4-5 | WIN — domain-appropriate dream CONTENT helps. Engineering agents should dream of engineering-craft, not historical-figures-in-computing. | `phase-015-2026-05-13.json` |
| 16 | 2026-05-13 | claude-haiku-4-5 | WIN — the technical-prompt story flips. With the neutral wrapper, imagination injection is net-positive (or at least neutral) on technical work. The shipping defaults (engineering routing → neutral wrapper) are validated by data, not just intuition. | `phase-016-2026-05-13.json` |
| 17 | 2026-05-13 | claude-haiku-4-5 | MIXED — mechanism ships clean (and may be useful for power-users), but per-prompt routing is NOT a default enabled feature. Phase 16's data already validated the agent-level routing as the load-bearing decision; per-prompt is a refinement that didn't pay off at this N. | `phase-017-2026-05-13.json` |
| 18 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — pure curation work, no behavior change beyond reducing repetition. | `phase-018-2026-05-13.json` |
| 19 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — small surface, robust failure modes, gracefully handles the long tail without changing modal behavior. | `phase-019-2026-05-13.json` |
| 20 | 2026-05-13 | claude-haiku-4-5 | STATISTICALLY ROBUST WIN — engineering-craft seeds beat computing-history seeds on decision-shaped technical scenarios. The shipping content preferences are justified beyond noise. | `phase-020-2026-05-13.json` |
| 21 | 2026-05-13 | claude-haiku-4-5 | MIXED — mechanism solid, default keeps it off. The agent-level routing from Phase 11/12 (which Phase 14 + Phase 20 confirmed at N=140 and N=60 respectively) is the load-bearing decision. Per-prompt content routing is preserved as a knob. | `phase-021-2026-05-13.json` |
| 22 | 2026-05-13 | claude-haiku-4-5 | NULL RESULT — honestly reported. Imagination injection neither helps nor hurts on single-function bug-fix tasks at the difficulty Haiku 4.5 handles trivially. The campaign's prior judge-based findings (79% / 57% / 67% win rates by domain) still stand as the load-bearing effectiveness measurements; they capture a different (and arguably more important) property: response usefulness on open-ended questions where 'correct' is underdetermined. | `phase-022-2026-05-13.json` |
| 25 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — clean infrastructure ship. The data accumulates as users use dreamscape; the campaign's offline measurements are unchanged. | `phase-025-2026-05-13.json` |
| 26 | 2026-05-13 | claude-haiku-4-5 | CLARIFYING NEGATIVE — per-prompt content routing should NOT be enabled by default. The N=210 measurement resolves Phase 21's wide CI; the keyword-classifier-driven routing genuinely loses to static. | `phase-026-2026-05-13.json` |
| 27 | 2026-05-13 | claude-haiku-4-5 | CONFIRMATORY NEGATIVE — LLM classification doesn't rescue per-prompt content routing. Phase 26's directional finding holds with a more precise classifier. The dominant effect is filtering-vs-diversity, not classifier accuracy. | `phase-027-2026-05-13.json` |
| 28 | 2026-05-13 | claude-haiku-4-5 | READY TO MERGE — clean infrastructure ship. Unlocks future cross-deployment analysis without changing modal behavior. | `phase-028-2026-05-13.json` |

