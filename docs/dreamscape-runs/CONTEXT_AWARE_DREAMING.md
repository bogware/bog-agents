# Deep dive — Context-aware dreaming

> Status: **partially implemented in this commit**, with concrete
> next-step recommendations to deepen the work. Designed against the
> Phase 10/11/12 evidence base. Sized so a future PR can extend
> without architectural changes.

## The premise

Phases 10 + 11 established that the imagination injection's effect
is **domain-conditional**: dreams help on creative/design prompts
(P11: 6/7 treatment wins) and either hurt or are noisy on
technical-debugging prompts (P10: 1/7, P12: 5/7 — net unclear, but
trending less helpful). The same dream content, the same wrapper —
opposite outcomes by prompt class.

The natural conclusion: dreamscape should **adapt the dream content
+ wrapper to the agent's working domain**. An engineering agent
shouldn't dream of dragons in a moonlit forest if the user is going
to be asking about race conditions; a designer's agent shouldn't
dream of compiler internals if the user is going to be asking about
microcopy voice.

This document describes (a) what shipped in this commit, (b) what's
realistic for a v2 PR, and (c) the experiments that would justify
each layer.

## What shipped in this commit

A minimal, deterministic, keyword-based domain classifier with two
routing surfaces:

### 1. `bog_agents_cli/dreamscape/domain.py` (new, 290 lines)

```
classify_agent_domain(profile: str)
    -> Literal["engineering", "creative", "research", "general"]
```

Pure-function keyword scoring. Each domain has a hand-curated
vocabulary (~40-50 keywords per domain). The classifier:

* Counts distinct keyword-types hit per domain (a profile that says
  "code" 50 times doesn't dominate one that says "code, refactor,
  debug").
* Picks the strongest signal.
* Falls back to `"general"` if the total signal is below
  `_MIN_KEYWORD_HITS=2`, OR if the top-2 are within 1 hit of each
  other (ambiguity guard).

Persistence:

* `capture_agent_profile(agent_id, system_prompt)` writes the
  agent's resolved system prompt to
  `~/.bog-agents/agents/<id>/agent_profile.txt`, truncated at 8KB.
* Called once at agent build time inside
  `_attach_dreamscape_middleware`.

### 2. Two routing hooks

**Hook A — seed-category preference.** In `dream_engine.generate_dream`:

```python
domain = resolve_agent_domain(agent_id)
if domain != "general":
    chosen_categories = preferred_seed_categories(
        domain, available=seeds.list_categories()
    )
else:
    chosen_categories = cfg.seed_categories
```

The mapping is:

| Domain | Preferred categories (ordered) |
|---|---|
| engineering | computing-history → history → space |
| creative | myth → nature → history → space → computing-history |
| research | history → computing-history → space → nature |
| general | all 5 categories (uniform; matches v1 behavior) |

An engineering agent now dreams primarily of Knuth interrupting
TAOCP for ten years to write TeX and Doug Engelbart demoing the
mouse in 1968 — language that resonates with the agent's working
context. A creative agent still gets the full Mímir's-well +
Anansi-the-spider mix that Phase 11 rewards.

**Hook B — injection wrapper style.** In `_attach_dreamscape_middleware`,
when the imagination middleware is being constructed:

```python
domain = resolve_agent_domain(safe_id)
preferred_style = recommended_injection_style(domain)
# "creative" -> "dreams"; everyone else -> "neutral"
effective_cfg = dc_replace(cfg.imagination, injection_style=preferred_style)
middleware_list.append(ImaginationMiddleware(agent_id=safe_id, cfg=effective_cfg))
```

The neutral wrapper (Phase 12) doesn't measurably outperform the
dreams wrapper on technical prompts, but it removes the irritant
that Phase 10's judge flagged (*"Fragment 2 from your dreams"* feels
out of place when the user is debugging chmod). For creative agents,
the dreams wrapper stays because Phase 11's win-rate is much
stronger with it.

### 3. Tests

11 new unit tests under `TestDomainClassifier`:

* Engineering / creative / research profiles classify correctly.
* Empty + low-signal + ambiguous profiles all fall back to
  `"general"`.
* Profile capture round-trips through disk.
* Profile truncation at the 8KB cap.
* `preferred_seed_categories` filters by available categories.
* `recommended_injection_style` maps correctly per domain.

## What's NOT shipped (and why)

Four deliberate omissions, each with a future-PR shape:

### A. LLM-based classifier

Keyword scoring is brittle. A profile that uses unusual vocabulary
(non-English keywords, niche jargon, code-only system prompt) will
miss-classify or fall back to `general`. An LLM-based classifier
(one Haiku call per agent build, cached on disk) would be more
robust.

**Trade-off:** added latency at agent build (~1-2s for a Haiku call)
and per-build cost (~$0.001). The keyword classifier is zero-cost
and instant.

**Recommendation:** ship the keyword version (which we did).
Add the LLM fallback in a future PR as
`classify_agent_domain_llm(profile, model)` that's invoked only
when the keyword classifier returns `"general"`. That gives us the
best of both — cheap deterministic for the modal case, principled
fallback for the long tail.

### B. Per-domain seed library expansion

Today's 50-seed library is mixed across 5 categories. The
`engineering`-domain mapping currently re-weights toward
`computing-history`, but that category only has 10 entries. An
engineering agent doing 30 dreams will start repeating.

The future-PR shape: add new categories like `engineering-craft`
(software-engineering-specific seeds — "git bisect on a 3-year-old
regression," "the assertion that fired only at midnight UTC,"
"the deprecated API still in production six years after EOL"),
`design-process` (creative-specific), `research-method`
(research-specific).

Concretely:

```
seeds/_SEEDS = {
    "nature": (...),           # 10 (existing)
    "space": (...),            # 10
    "history": (...),          # 10
    "myth": (...),             # 10
    "computing-history": (...), # 10
    "engineering-craft": (...), # NEW — target 10
    "design-process": (...),    # NEW — target 10
    "research-method": (...),   # NEW — target 10
}
```

Then update `_DOMAIN_SEED_PREFERENCES` to favor the new categories
per domain.

**Cost:** purely curation work. No code changes beyond appending
to `_SEEDS`. Estimate: 30 new high-quality seeds at maybe 30
minutes of focused writing.

### C. Per-domain dream prompt templates

Today there's exactly one `DREAM_AUTO_SYSTEM_PROMPT` — the same
prompt is used regardless of domain. It tells the model to
"produce a short, vivid imaginative fragment." For a creative agent
that's the right ask. For an engineering agent we could ask for
something like *"produce a short, evocative observation about a
problem you've encountered before — one sentence is enough — and
explain in two paragraphs how it might inform unrelated work."*

The future-PR shape:

```python
DREAM_PROMPTS: dict[Domain, str] = {
    "general": DREAM_AUTO_SYSTEM_PROMPT,          # existing
    "engineering": DREAM_AUTO_ENGINEERING_PROMPT, # new
    "creative": DREAM_AUTO_CREATIVE_PROMPT,       # new
    "research": DREAM_AUTO_RESEARCH_PROMPT,       # new
}
```

**Trade-off:** more surface area to maintain. The single-prompt
approach is easier to keep coherent. Worth doing only if Phase 14+
data shows per-domain prompts produce measurably different output
quality.

### D. Continuous profile refinement

The profile is captured once at agent build time. If the user's
work shifts mid-session (e.g. they pivot from debugging to
documentation), the classification stays stale. A v2 could re-classify
on every N model calls, using the recent message history as the
profile signal.

**Trade-off:** complexity. The agent's working domain rarely
changes drastically within one CLI session. The cost-benefit
favors the simple one-shot capture for now.

## Limitations of the current implementation

1. **Keyword vocabularies are English-only and bias toward
   western software culture.** A future PR should add multi-language
   support or use a multilingual embedding-based classifier.
2. **The ambiguity guard is conservative.** A profile that
   genuinely spans engineering + research (the bog-agents CLI's
   system prompt would qualify) falls back to `general`. That's
   the safer failure mode but it costs us a routing opportunity.
3. **No validation telemetry yet.** When the classifier picks
   `engineering` for an agent, we have no signal whether that was
   the right call. A `/agent-state` extension could surface the
   classified domain so a human can spot-check.
4. **The Phase 10/12 variance hasn't been fully resolved.** We're
   shipping the "neutral wrapper for engineering" decision on
   weakish evidence (Phase 12: dreams 5/7, neutral 4/7 — within
   noise). A larger trial would tighten the call.

## Suggested experiments to deepen this work

In rough priority order:

### Phase 13 — Routing verification
Run a mixed scenario set (3 creative + 4 technical) against ONE
agent whose profile has been captured as engineering. Verify:

* The classifier picks `engineering` (it should).
* The imagination injection uses the `neutral` wrapper on every
  call (it should).
* The seed selection draws from `computing-history → history →
  space` (it should).
* The judge's verdict on the creative-prompt subset is *no worse*
  than control (the absence of "dreams" framing shouldn't
  catastrophically hurt creative output on this agent).

If the engineering-classified agent does *worse* on creative
prompts than a general-classified agent would, we've over-routed —
the engineering classification should not break creative work
entirely.

### Phase 14 — N=30 statistical power
Re-run Phase 10 + 11 + 12 with N=30 trials per condition. The
qualitative story is clear at N=7; we should know the true
effect-size before tuning more knobs.

### Phase 15 — Engineering-craft seed library
Write 30+ engineering-craft seeds (the "git bisect at midnight"
shape). Re-run Phase 12 with the new seeds enabled for the
engineering-classified arm. Hypothesis: domain-appropriate seeds
will produce treatment wins on technical-debugging prompts even
when the wrapper is the dreams style.

### Phase 16 — LLM classifier fallback
Implement `classify_agent_domain_llm` for the cases where the
keyword classifier falls back to `general`. Compare classifier
agreement on a labeled sample of 50+ real agent system prompts.

### Phase 17 — Continuous profile refinement
Track recent prompt themes per session. Re-classify on every N
messages and update the routing dynamically. Measure: how often
does the dynamic classification change vs the static one?

## Cost of the work shipped

The engineering work in this commit is **zero LLM cost at runtime**:
keyword classification + on-disk caching + a single profile write
at agent build. The Phase 11 + 12 experiments that justified this
work cost ~$0.11 total.

## Final recommendation: ship it

The minimal context-aware layer is shipped, tested, and the
defaults are conservative (general fallback dreams uniformly across
all 5 categories with the dreams wrapper — exactly the v1 behavior).
Agents that *can* be classified to a specific domain get
domain-routed seed selection + a domain-appropriate injection
wrapper. No regression risk for users who don't opt in to the
dreamscape feature set at all (the master switch still gates
everything).

The four omissions above are deliberate deferrals, not bugs.
Each has a clear future-PR shape and is justified by the data we
have (or, in two cases, the data we don't yet have).
