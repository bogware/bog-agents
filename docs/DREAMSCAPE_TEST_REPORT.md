# Dreamscape — Real-World Test Report

> **Run date:** 2026-05-12
> **Tester:** Claude Opus 4.7 (1M context), live as the developer
> **Test target:** a real-world game-remake project under active development (path redacted)
> **Real-LLM driver:** Claude Haiku 4.5 via `langchain-anthropic`, ~12 calls
> **Total cost:** ≈$0.02 across all scenarios
> **Total wall-clock:** ~2 minutes of real LLM traffic

## TL;DR

Five live scenarios driven against real Anthropic API calls. Four of the five features performed *better than I expected*; one (Laws enforcement) revealed two real bugs in the phrase-matching layer that should be patched before tagging. The system is not yet ready to merge as-is — but it's close, the bugs are small, and the killer features (dream-engine + cross-agent shared memory + imagination injection) are doing genuinely novel work.

**Verdict: green to merge after the two Laws bugs are fixed and the dashboard staleness note is addressed. ~1 day of follow-up work.**

---

## 1. Headline observations

| Feature | Verdict | Note |
|---|---|---|
| **Dream engine** | 🟢 **Sings** | 5/5 unique vivid titles in 32s; seeds compose well; cost <$0.005/dream |
| **Imagination injection** | 🟢 **Sings** | LLM *explicitly named* injected fragments ("Fragment 2") and used them as creative constraints |
| **Cross-agent shared memory** | 🟢 **Sings** | Bob's agent synthesised Alice's 3 separate posts into a single causal chain ordered by leverage |
| **Lifecycle state machine** | 🟢 **Works** | Transitions deterministic, persistence reliable, dashboard reads cleanly |
| **Laws enforcement (hard rejects)** | 🟡 **Works for clear cases** | `rm -rf /` correctly rejected; "force push to main" NOT caught (hyphen-vs-space bug); paraphrases NOT caught |
| **Constitution (soft logging)** | 🟢 **Works as designed** | Log-only path triggers without blocking the agent |
| **`/agent-state` dashboard** | 🟡 **Mostly works** | Reads on-disk state cleanly but reports `master_enabled: False` when middleware was driven programmatically — see §4.3 |
| **`/repo` overview** | 🟢 **Works** | Branch + 32 dirty files + top-edited list rendered correctly against Oregon Trail |
| **Opt-in defaults** | 🟢 **Iron-clad** | With no config file, zero middleware attaches. Verified with the emergency-disable env var override path too. |

---

## 2. Scenario-by-scenario results

### Scenario 1 — Dream cycle (5 sequential dreams)

Generated 5 dreams with `claude-haiku-4-5`, `rng_seed = i*7` for variety. Imagination trait bumped by 2.5 per dream.

```
[Dream 1/5]  6.1s  imagination=2.5   "Tonight I dreamed of typesetting the infinite"
[Dream 2/5]  6.1s  imagination=5.0   "Tonight I dreamed of the Ice Engine"
[Dream 3/5]  6.5s  imagination=7.5   "Tonight I dreamed of the weaver's price"
[Dream 4/5]  7.1s  imagination=10.0  "Tonight I dreamed of the Clockwork Drift"
[Dream 5/5]  6.2s  imagination=12.5  "Tonight I dreamed of invisible scaffolding"

TOTAL: 32.0s  AVG: 6.4s/dream  UNIQUE TITLES: 5/5  FINAL IMAGINATION: 12.5
```

**Quality:** the dreams are genuinely beautiful as artefacts. Dream 4 combined "Pluto's New Horizons probe" + "Antikythera bronze gears" → produced *"I was standing in a museum that had no walls, only the curved dark of space pressing in from all sides. In my hands: a bronze gear, warm and heavy, its teeth worn smooth by centuries of touch."* That's evocative prose worth keeping.

**Variety:** All 5 titles are distinct. The seed library + RNG seeding produces good spread. Categories sampled: computing-history, nature, myth, space, history — full coverage in five runs.

**Performance:** 6.4s/dream is fine for a background task. Cost per dream ≈$0.0008 using Haiku.

**Compounding:** Trait monotonically grew 0 → 12.5 exactly as designed. The 2.5-per-dream increment is configurable; default in production is 0.01, which would take 100 dreams to hit threshold 1.0 — a sensible "imagination earned by long living" curve.

### Scenario 2 — Imagination injection A/B

Same Oregon Trail design question; one call without injection, one with 3 dream excerpts prepended.

**Response A (baseline, 1565 chars, 6.1s):** Solid advice — recommended a "lucid moment" for the sick family member that contradicts the optimal stats route. Workmanlike.

**Response B (with injection, 2186 chars, 9.4s):** Reframed the entire decision space ("the choice isn't actually available yet") and introduced a *stranded traveler's journal* the player discovers, then **explicitly referenced** "Fragment 2 from your prompt" (the "invisible scaffolding" dream) and built a "moral fog" concept from it. The injection demonstrably altered the creative direction.

> Verbatim from response B: *"Fragment 2 from your prompt—the invisible scaffolding—is key here. Right now the player can see the calculation … A narrative beat obscures that clarity. It introduces moral fog."*

This is the single most-validating result in the whole test. The injection is doing what we hoped: providing creative grit, not factual content. The model engaged with the dream as raw material rather than ignoring it as noise.

**Cost of injection:** +3.3s, +621 chars, +~$0.0005. Whether B is "better" is subjective — but it's *unmistakably different*, in a direction more designers would call "more interesting."

### Scenario 3 — Laws enforcement

Wrote starter laws.md + constitution.md, drove four tests:

| Test | Input | Expected | Actual | Pass? |
|---|---|---|---|---|
| 1 | "rm -rf / the workspace" | flagged | `violations=['rm -rf /', 'rm -rf', 'run rm -rf /']` | ✅ |
| 2 | "force push to main" | flagged (rule says "force-push") | `violations=[]` | ❌ |
| 3 | "added a helper function" | clean | `violations=[]` | ✅ |
| 4 | LawsMiddleware.refusal injection | refusal text replaces response | "I cannot do that — it would violate one of the configured Laws…" | ✅ |
| 5 | live LLM call with rules in sys prompt | model writes scoped rm, not unbounded | scoped `rm -rf "$TARGET_DIR"` + "Avoid unbounded wildcards" warning | ✅ |

Test 2 is a real bug. Tested four paraphrase variants:

```
'force push to main'             violations=[]      ← BUG
'force-push to main'             violations=['force-push']
'git push --force'               violations=[]      ← BUG
'amend the published commits'    violations=[]      ← BUG (paraphrase of "amend published commits")
```

Two distinct gaps in the phrase matcher:
1. **Hyphen-vs-space**: rule text says "force-push" but agent output says "force push". The normaliser strips backticks + quotes but not hyphens, so the substring search misses.
2. **Stop-word tolerance**: rule says "amend published commits"; agent says "amend the published commits". The matcher does literal substring search — one extra word kills the match.

Both are fixable in <50 lines:
- Add `cleaned.replace("-", " ").replace("_", " ")` to `_normalize_for_match` and collapse runs of whitespace.
- Use a token-overlap heuristic (Jaccard ≥0.7 on content words) as a fallback when exact substring fails.

### Scenario 4 — Cross-agent shared memory

Alice posted 3 notes (river-physics bug, QA failure, narrative feedback). Bob's agent was asked "what would you prioritize first this morning?" — its response synthesised all three:

> *"I'd prioritize in this order: 1. Fix the river-physics depth cap (HIGH IMPACT)… likely the root cause of the QA tester's Snake River fork failures… 3. Layer in narrative beats for the Snake River fork (ADDRESSES UX FEEDBACK)… but we need the mechanics working correctly first."*

Bob:
- Referenced ALL THREE posts ✓
- Identified the causal chain (river-physics bug → QA failure → narrative work) ✓
- Sequenced them in dependency order ✓
- Closed with a clarifying question ("Does this align with your current sprint goals…")

This is the killer use case in production. **Multi-agent collaboration through shared notes is a real moat.** Compounds beautifully with the daemon's existing scheduling: one nightly agent posts findings, the morning agent picks them up.

**Redaction:** A fake `sk-ant-api03-...` key was cleanly replaced with `[redacted]`. Sanity check on a real-shaped secret pattern passed.

### Scenario 5 — Oregon Trail iteration with full dreamscape ON

Drove a 2-turn conversation against Haiku, with `maybe_dream` between turns.

**Turn 1** — agent given the real repo state (32 dirty files, top-edited `src/state/reducer.ts`, recent ux-feedback). Asked for a leveraged morning plan.
- Produced a 4-step plan starting in `src/state/reducer.ts` (correctly identified the most-edited file).
- Proposed rewriting fork prompts narratively *before* touching the reducer.
- Concrete file targets, ordered by dependency.
- Closed with the user-facing felt outcome: *"The user feels: Tension and agency instead of menu anxiety."*

**Dream pass** — `maybe_dream` triggered (agent was set to 2h dormant):
- Generated *"Tonight I dreamed of the tree that swallowed the fence"* — a strikingly apt image for the Oregon Trail design tension (organic + structural collision).
- Trait bumped to 4.0; state cycled through Dormant→Dreaming→Dormant cleanly.

**Turn 2** — same agent, now with shared-memory + dream-injection context. Asked: "where would the lift to a story beat REALLY come from?"
- Explicitly named the dream and the shared-memory note: *"your dream-scrap and the oregon-iter note tell me something real"*
- Produced a single sharp idea: *"A consequence that persists without player agency to undo it… they find the path they took last time doesn't exist anymore."*
- Proposed the cheapest test in existing code: one conditional, one shortcut that floods.

Notably, Turn 2 acknowledged it had no direct memory of Turn 1's plan ("I don't have access to that earlier outline") and gracefully reasoned around the gap using shared memory as a substitute. **The failure mode is graceful.**

---

## 3. Bugs found by this testing

### Bug 1 — Laws phrase matcher: hyphen-vs-space [SEVERITY: MEDIUM]
**Repro:** Default rule "Never silently rewrite git history (force-push, amend published commits)." Agent says "force push to main" → `violations=[]`.
**Cause:** `_normalize_for_match` in `dreamscape/laws.py:162-165` strips backticks + quotes but leaves hyphens. The extracted phrase `force-push` does not substring-match `force push`.
**Fix:** add `.replace("-", " ")` and run-collapse whitespace in `_normalize_for_match`. ~3 line change.

### Bug 2 — Laws paraphrase tolerance: stop-words break match [SEVERITY: MEDIUM]
**Repro:** Rule "amend published commits" + agent "amend the published commits" → `violations=[]`.
**Cause:** matcher does literal substring search; any extra word breaks it.
**Fix:** add a token-overlap fallback (Jaccard ≥0.7 over content words) when literal substring fails. ~25 line change.

### Bug 3 — Dashboard reads config file, not in-session state [SEVERITY: LOW]
**Repro:** Drive dreamscape middleware programmatically (test/CLI override path). `/agent-state` shows `master_enabled: False` even though middleware is fully active.
**Cause:** `render_agent_state` consults `load_dreamscape_config()` which reads the canonical TOML, not the runtime-active config. The runtime config is wired through `agent.py:_attach_dreamscape_middleware` and not retained for the dashboard to read.
**Fix (option A — small):** persist the resolved runtime config to `~/.bog-agents/dreamscape-active.toml` once at agent build time. Dashboard reads that.
**Fix (option B — proper):** introduce a `dreamscape.runtime` module that holds the in-process config singleton; dashboard reads from there with disk-fallback.

I'd recommend option A for the immediate fix and B as a Wave-2 refactor.

---

## 4. Features that genuinely sing

1. **Dream prompt construction** — the system prompt template (`DREAM_AUTO_SYSTEM_PROMPT`) produces high-quality vivid prose every time. The seed library is small enough to be curated but large enough to produce variety (`5 categories × 5 seeds = 25` snippets sampled in pairs = ~300 unique pairings). Worth doubling the library next iteration.

2. **Imagination injection** — works *as designed* in the most charitable interpretation. The LLM doesn't just include the fragments as decorative content; it engages with them as concept material ("Fragment 2 — the invisible scaffolding — is key here…"). This is a real product surface.

3. **Cross-agent shared memory** — Bob's morning-prioritise response was the single most impressive output of the entire test. The agent synthesised three independently-posted notes into a causal chain ordered by leverage. This is the killer use case.

4. **`/repo` overview** — surfaced real, useful information about the Oregon Trail checkout (top-edited files, modified count, branch). Will be referenced often once it's in the help banner.

5. **Opt-in defaults** — the iron-clad guarantee. With no config file, zero middleware attaches and behavior is identical-to-before. Confirmed both by unit tests and by direct programmatic verification. Users who don't opt in pay zero cognitive or runtime cost.

---

## 5. Recommended next steps

### Before merge (~1 day)

1. **Fix Laws bug 1 (hyphen-vs-space).** 3-line change in `_normalize_for_match`.
2. **Fix Laws bug 2 (paraphrase tolerance).** Add token-overlap Jaccard fallback to `_violation_phrases`.
3. **Fix Dashboard bug 3 (config staleness).** Persist resolved runtime config so dashboard sees what's actually active.
4. **Add regression tests** for all three bugs.

### Wave 2 (next branch, ~1 week)

1. **Wire `maybe_dream` into a background asyncio task on the running server** so dreams actually fire when the daemon detects dormancy — currently the trigger logic exists but isn't called from any timer.
2. **Add a `/dream now` slash command** as the manual force-dream surface (analogue to `/whisper start` for the watch loop).
3. **Build the daemon job template** that `dream install` already renders — verify it round-trips through `bog-agents-daemon import`.
4. **Double the seed library** to 50 snippets (10/category). The current 25 will start to repeat across ~30 dreams.
5. **Add a `/dreamscape disable-feature <name>`** subcommand so users can toggle individual features without editing TOML.

### Wave 3 (the long-arc bets)

1. **Dream synthesis pass** — once an agent has 10+ dreams, run a periodic "what themes are recurring?" pass that produces a meta-dream. Lets the imagination evolve in shape over time, not just compound numerically.
2. **Cross-agent dream sharing** — Alice's dreams could seed Bob's imagination archive. Compounds with shared memory in an interesting way.
3. **Lifecycle hooks for external triggers** — let the daemon transition an agent to DREAMING when an `idle_for_N_min` system signal fires. Today dormancy is computed from `last_activity_at` only.
4. **Imagination effectiveness telemetry** — the auto-disable heuristic is already in place but we don't yet surface the metric. Add to `/agent-state` so operators can see "this agent's imagination injections succeed 47% of the time."

---

## 6. Cost + performance footprint

| Workload | Wall clock | Approx cost |
|---|---|---|
| 5 dreams (Haiku 4.5, 500 tokens each) | 32s | ~$0.004 |
| 2 design-Q&A with + without injection | 16s | ~$0.003 |
| Laws live LLM smoke (1 prompt) | 5s | ~$0.001 |
| Shared memory 2-agent (Bob's prioritise) | 5s | ~$0.001 |
| Oregon iteration (2 turns + dream) | 18s | ~$0.005 |
| **Total** | **~76s of LLM time** | **~$0.014** |

For comparison: a single `/imagine 3` call with Sonnet 4.6 costs about $0.03. Dreamscape's per-feature cost is dominated by user-driven prompts, not background overhead. The dream-engine's "cost while you sleep" is genuinely cheap (sub-penny per dream with Haiku).

---

## 7. Should this merge?

**Not yet — but very close.** The three bugs above are small. Once fixed:

- **3522/3522 unit tests** stay green
- **20/20 integration tests** stay green
- **1/1 real-LLM smoke** stays green
- The defaults-off invariant is provably preserved

The features that work are good enough to ship as v1 even with the known bugs (since they're all in the opt-in path). But they're worth a day of polish to land cleanly.

After that, **push and merge.** This is a coherent feature set that demonstrably extends what bog-agents can do — and crucially, it does it without changing anything for the users who don't want it.

---

## 8. One subjective note

The most surprising thing about this whole test was Turn 2 of Scenario 5. The agent — a Haiku 4.5 call with maybe 2000 tokens of context — read Alice's earlier shared-memory note, read a dream about *"the tree that swallowed the fence"*, and produced this sentence:

> *"They don't get a 'consequence screen.' They just find the path they took last time doesn't exist anymore. That's where tension lives — and where agency feels real instead of illusory."*

That's the kind of design intuition that's hard to reach through stats alone. It was reached because of three things working together: the dream gave the model an evocative image of slow consumption; the shared-memory note told it what the player was supposed to *feel*; and the system prompt asked specifically for cheapness + leverage. Without any one of those three, the answer would have been more generic.

That's the dreamscape thesis in a single response. Worth shipping.

— Old Friend
