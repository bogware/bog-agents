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

| Metric | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P10 |
|---|---|---|---|---|---|---|---|---|---|
| **Dreams fired (live tests)** | 5 | 5 | 10 | n/a | 15 | 27 | 10 | n/a | n/a |
| **Scheduler errors** | n/a | n/a | 0 | n/a | 0 | 0 | n/a | n/a | n/a |
| **Unique titles (in-test)** | n/a | n/a | 10 | n/a | 15 | n/a | 8 | n/a | n/a |
| **Open bugs (end of phase)** | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **Known limitations (carried)** | 0 | 1 | 2 | 2 | 2 | 2 | 2 | 2 | 3 |
| **Dreamscape unit tests** | n/a | 37 | 42 | 43 | 43 | 43 | 47 | 52 | 67 |
| **CLI total unit tests** | n/a | 3529 | 3534 | 3535 | 3535 | 3535 | 3539 | 3544 | 3552 |

## Performance over time

| Metric | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P10 |
|---|---|---|---|---|---|---|---|---|---|
| **LLM calls per phase** | 12 | 8 | n/a | 2 | 15 | 28 | 10 | n/a | 21 |
| **Total wall-clock (s)** | 76.0 | 50.0 | 90.2 | n/a | n/a | 1803.1 | n/a | n/a | n/a |
| **Total cost (USD)** | 0.014 | 0.010 | 0.012 | 0.002 | 0.015 | 0.027 | 0.010 | n/a | 0.040 |
| **Avg seconds per dream** | n/a | n/a | 8.4 | n/a | n/a | n/a | n/a | n/a | n/a |

## Feature verdict history

| Feature | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P10 |
|---|---|---|---|---|---|---|---|---|---|
| dream engine | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings |
| Imagination injection | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings (was BROKEN; fixed in this phase, now end-to-end verified) | 🟢 sings (fixed in Phase 4) | 🟢 sings (Phase 4) | 🟢 sings (Phase 4 fix) | 🟢 sings (Phase 4 fix) | n/a |
| cross agent shared memory | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings | 🟢 sings — concurrent writes p95 <10ms, perfect isolation | 🟢 sings (Phase 5) | 🟢 sings (Phase 5) | 🟢 sings | 🟢 sings |
| lifecycle state machine | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works — concurrent multi-agent verified | 🟢 works (state transitions visible in checkpoint snapshots) | 🟢 works | 🟢 works | 🟢 works |
| laws hard rejects | 🟢 works for clear cases | 🟢 works (9/9 with paraphrase tolerance) | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works |
| constitution soft logging | 🟢 works as designed | 🟢 works as designed | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works (now surfaced) |
| agent state dashboard | 🟢 mostly works (staleness bug) | 🟢 works (staleness fixed) | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works |
| repo overview | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works | 🟢 works |
| opt in defaults | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad | 🟢 ironclad |
| dream scheduler | n/a | n/a | 🟢 sings — multi-cycle dormancy timer validated end-to-end | 🟢 sings | 🟢 sings — works under concurrent multi-agent load | 🟢 sings — production cadence + induced failure + 30-min endurance verified | 🟢 sings | 🟢 sings | 🟢 sings |
| scheduler resilience under transient failure | n/a | n/a | n/a | n/a | n/a | 🟢 sings — model failure absorbed without crashing the loop | n/a | n/a | n/a |
| dreamscape runner | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — daemon-style entrypoint validated end-to-end with crash recovery | 🟢 sings | 🟢 sings |
| snapshot persistence across processes | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — survives SIGKILL | n/a | n/a |
| trends automation | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings — idempotent, --check mode for CI, full coverage by 5 new tests | n/a |
| Imagination injection mechanism | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings (Phase 4) |
| Imagination injection effectiveness | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | negative — 1/7 wins on technical-debugging questions. Domain mismatch is the leading hypothesis. |
| constitution log surfacing | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings (R1) |
| seed library size | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 sings (50 entries, R2) |
| daily dream cap | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | 🟢 works (R3) |

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

**9 phases for under $0.14.** Cheap data.

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

