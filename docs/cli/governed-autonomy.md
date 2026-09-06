# Governed autonomy

The point of Bog Agents is not that an agent *can* run on its own — most can.
It's that you can trust the run you didn't watch. Every autonomous surface is
bounded by hard cost caps and can hand you proof of what it did.

This guide covers the surfaces that turn the engine up, and the guardrails that
make that safe: teams, best-of-N, the jury, the operator, agent-authored
workflows, cost caps, evidence bundles, and the pre-submit gate.

## The safety floor: cost caps

Nothing autonomous can fork-bomb your bill. Every team, sub-agent spawn, and web
search is counted against `RunawayCaps` (max sub-agents, max web searches, max
dollars), and a per-run budget **pauses** at the cap instead of crashing the
turn — you resume with a higher budget when you mean to.

Set the caps once in `config.toml` (the `cost.*` keys) or per environment:

```toml
[cost]
max_subagents = 12
max_web_searches = 40
budget_usd = 5.0            # a run pauses at $5; resume to continue
daily_ceiling_usd = 50.0    # the CLI stops starting new runs past $50/day
```

`/cost` shows spend this session per model and tool against the durable daily
ledger; `/usage` shows per-response token usage as it streams.

## `/team run` — a governed team over a task ledger

A team decomposes work onto a shared, atomic, dependency-aware **task ledger**.
Teammates claim only tasks whose dependencies are done, coordinate over a
**mailbox**, and stop when the ledger drains or a cap denies further work.

```text
/team run implement the OAuth refresh flow: split into API, storage, and tests;
          storage and tests depend on the API
```

The team appears in `/tasks` while it runs, so you can watch the board, steer a
teammate, pause, or kill it. Teammate file exchange (`send_file`, `send_patch`,
`receive_files`) lets one worker hand a fixture or a patch to another, DLP-scanned
on the way out.

## `/best-of-n` — N attempts, keep the winner

When a task has a clear success test but an uncertain path, run it several ways
and keep the best.

```text
/best-of-n 5 make tests/test_scheduler.py deterministic
```

Each of the N attempts runs in its own isolated git worktree, so they can't step
on each other. Every attempt is scored against a rubric; the winning worktree is
kept and the rest are discarded. Use it for flaky-test fixes, tricky refactors,
or anything where "did it actually work" is easy to check but hard to plan.

## `/jury` — a multi-model vote on a diff

```text
/jury                 # review git diff HEAD..
/jury staged          # review staged changes
/jury <ref>           # review git diff <ref>..HEAD
```

The juror models come from `[jury].models` in `~/.bog-agents/config.toml`; unset,
your active model votes three times as a quick self-review. Each juror returns a
verdict and the votes are aggregated into SHIP / FIX.

## `/self-review` — the pre-submit gate

```text
/self-review                 # five lenses over the current diff
/self-review --fix           # loop: review, fix, re-review until clean
/self-review --since-last    # only what changed since the last review
```

Five reviewer lenses — correctness, security, maintainability, tests, and
over-claims — run over your own diff and return SHIP / FIX-FIRST. It writes a
memo under `.bog-agents/self-review/` with a diff fingerprint, so a re-review
knows what already passed.

## `/operator` — route by difficulty

A cheap judge classifies each prompt `easy/medium/hard/max` and stages a one-turn
model + effort override, biased by your objective.

```text
/operator on
/operator objective intelligence     # or: balance | cost
```

Judge failures never block a turn — every path falls through to your active
model. Presets (anthropic, bedrock, local, hybrid) and per-tier overrides live in
`~/.bog-agents/operator.toml`.

## `/workflow` — agent-authored, saved as a slash command

Ask the agent to design a repeatable pipeline; it writes the YAML, Bog Agents
validates and saves it, and it becomes a first-class `/command`.

```text
/workflow author a release-readiness check: map the changes, review them,
          run the tests, and summarise
        # → validated and saved as /release-readiness
```

```text
/release-readiness v0.10        # run it
/workflow status release-readiness
/workflow resume release-readiness --budget 3    # continue a budget-paused run
```

A workflow is a short list of phases — `context`, `work`, `review`, `verify`,
`synthesize`. Each phase fans out as a governed team; review and verify phases
are gates whose tasks must end with a PASS verdict; runs persist per-phase, so a
budget pause or a failure resumes at the first unfinished phase instead of
starting over. `{arg}` and `{context}` placeholders thread arguments and earlier
phases' results through the prompts.

The agent can also author one for you through the `author_workflow` tool once a
project has a `.bog-agents/workflows/` directory (or `tools.workflows` is on).

## Plan, review, then execute — headless

For unattended runs where you still want a human's eyes on the plan:

```bash
bog-agents --plan "add rate limiting to the public API"          # prints a read-only plan
bog-agents --plan "add rate limiting to the public API" --auto   # plans, then executes the plan
```

The planning pass runs a `plan_only` agent that has no write / edit / execute /
git-mutation tools at all — it physically cannot change anything — then the second
pass executes the approved plan under acceptEdits (or `--auto-approve`).
Interactively, `/review-plan` opens the same plan (a butcher manifest, a JTBD
spec, plan-mode output, or a file) for line-by-line comments and slice
checkboxes before you approve or send it back for revision.

## Proof-of-work: evidence bundles

An autonomous change is worth more with proof attached. The SDK's
`bog_agents.evidence` collects the diff stat, the output of your verify command,
and the rubric verdict into a bundle whose `merge_ready` gates on both checks and
rubric. The CLI appends it to a PR body:

```bash
bog-agents -n "fix the auth bug and open a PR" --pr --pr-evidence
```

Now "it passed" is a document a reviewer can read, not a claim they have to take
on faith.

## See also

- [Findings & security scans](findings.md) — the ledger and CI gate
- [Governance & safety](governance.md) — trust profiles, managed policy, sandbox
- [Command reference](commands.md)
