# Expert Rules

> A forward+backward-chaining rule engine. Asserts a `tool_call` fact
> before every tool call. Rules can deny, modify, route, or require
> approval. CLI surface: `/expert`, `/why`, `/prove`.

## When you reach for this

You've been using bog-agents for a while. You've noticed patterns:

- "Every time the agent tries to push to main, I want to pause and
  think."
- "After a `pip install` of an unfamiliar package, I want it to ask
  before running the new code."
- "Cost over $5 in a session, surface an approval before each model
  call."
- "Never run a shell command that includes `rm -rf` outside of
  `/tmp/`."

The expert rules engine is for codifying those. YAML files in
`.bog-agents/expert_rules/`. The engine loads them, asserts a
`tool_call` fact before every tool call, runs the rules, and either
allows the call, denies it, modifies its args, routes it, or surfaces
an approval prompt.

It composes with `RulesMiddleware` (the prose injector — different
feature) but is independent. It does *not* replace HITL — it's a
layer that decides which tool calls reach HITL in the first place.

## The 30-second tour

`.bog-agents/expert_rules/no-force-push.yaml`:

```yaml
- name: block_force_push_to_main
  description: Block force-pushes to main/master.
  salience: 100
  when:
    - tool_call:
        name: shell_execute
        command:
          matches: 'git push.*--force.*(main|master)'
  then:
    - deny: "Force-push to main/master is prohibited by policy."
    - audit_log:
        event: prod_force_push_blocked
```

Enable expert mode:

```text
/expert on
```

The next time the model tries `git push --force origin main`, the
engine denies the call. The model sees the denial and adapts.

## Anatomy of a rule

```yaml
- name: <kebab-case-identifier>
  description: Plain-English one-liner.
  salience: 0-100             # higher fires first in the same iteration
  once: false                 # if true, fires at most once per /run
  when:                       # list of patterns; ALL must match
    - <fact-type>:
        <field>: <value>
        <field>:
          matches: <regex>
        <field>:
          gt: <number>
  then:                       # list of actions to fire on match
    - deny: "<reason>"
    - audit_log: { event: ..., metadata: {...} }
    - notify: { channel: ..., message: ... }
    - require_approval: { gate: "...", risk: low|medium|high }
    - modify: { command: "..." }      # rewrite the tool's args
    - route: <subagent-name>          # delegate to a different sub-agent
    - assert: <new-fact>              # forward-chain a new fact
```

### Patterns

| Match type | Example |
|---|---|
| Equality | `command: "ls"` |
| Regex | `command: { matches: "^git push" }` |
| Comparison | `cost_usd: { gt: 5.0 }` |
| Set membership | `tool: { in: [edit_file, write_file] }` |
| Existence | `args.path: { exists: true }` |
| Negation | `tool: { not: shell_execute }` |

Multiple fields under one fact type are AND-ed. Multiple `when:`
items are AND-ed. To express OR, write multiple rules or use the
`any:` keyword.

### Actions

| Action | What it does |
|---|---|
| `deny: "<reason>"` | Tell the model the tool call is refused with the given reason. The agent sees the reason and adapts. |
| `require_approval: { gate, risk }` | Surface an approval dialog with the given prompt. |
| `audit_log: { event, metadata }` | Write a structured entry to the audit trail. No effect on the call. |
| `notify: { channel, message }` | Fire a notification (Slack / webhook / etc., via `LifecycleHooksMiddleware`). |
| `modify: { <field>: <new-value> }` | Rewrite the tool call's args before it executes. |
| `route: <subagent>` | Hand the call to a named sub-agent. |
| `assert: <fact>` | Add a fact to working memory. Lets you chain rules. |

## How the engine runs

1. The agent's about to make a tool call.
2. The middleware asserts a `tool_call` fact with the tool name and
   args.
3. The engine iterates: find every rule whose `when:` patterns match
   working memory.
4. The conflict set is sorted by salience (high first), then
   by most-recent-matched-fact, then by rule name (stable).
5. Each rule fires its `then:` actions in order.
6. If any action mutates memory (e.g. `assert:`), the loop restarts.
7. Hard cap at 200 iterations to prevent infinite loops; a cycle
   trace entry gets recorded if it hits.

The engine is **deterministic**: same facts + same rules = same
output. Snapshot the trace and compare across runs.

## CLI surface

```text
/expert                    # status (on/off, rules loaded, recent activations)
/expert on                 # enable the engine
/expert off                # disable (rules stay loaded, just bypassed)
/expert reload             # re-read all *.yaml in .bog-agents/expert_rules/
/expert lint               # validate every rule file without enabling
/expert wizard             # guided LLM-driven rule authoring
/expert write <intent>     # author a rule from a one-line intent
/expert watch start [interval] [--apply]    # background proposer loop
/expert watch stop
/why <tool-call-or-message>    # explain why a call was denied/approved/modified
/prove <rule-name>             # show that a rule fires for a given fact
```

`/why` is the killer feature for debugging. After a denial:

```text
> /why
The call to `shell_execute` with command `git push --force origin main`
was denied by rule `block_force_push_to_main`:
  - tool_call.name == "shell_execute"  ✓
  - tool_call.command matches "git push.*--force.*(main|master)"  ✓
  - salience 100; fired before block_arbitrary_pushes (salience 50)
  - deny reason: "Force-push to main/master is prohibited by policy."
```

`/prove` walks the rule backwards from a desired outcome:

```text
> /prove block_force_push_to_main
To fire `block_force_push_to_main`, working memory must contain:
  - tool_call where name="shell_execute" AND command matches "git push.*--force.*(main|master)"
Example fact that would fire it:
  tool_call(name="shell_execute", command="git push --force origin main")
```

## Writing rules well

### Be specific in `when:`

```yaml
# Bad: matches every shell command
when:
  - tool_call:
      name: shell_execute

# Good: matches the specific footgun
when:
  - tool_call:
      name: shell_execute
      command:
        matches: 'rm\s+-rf\s+(?!/tmp/|/var/tmp/)'
```

Over-broad rules cause the engine to deny things you didn't mean to
deny. The model sees a wall of denials and starts to flail.

### Use `salience` to order independent rules

When two rules could both fire, the one with higher salience wins.
Convention: 100 = security-critical, 50 = workflow guidance, 10 =
nice-to-have.

### Use `once: true` for noisy rules

A rule that audit-logs every shell call doesn't need to fire 50
times per session. `once: true` caps it at one activation per
`/run`.

### Keep `then:` minimal

A rule with five actions is harder to debug than five rules with
one action each. Trace logs name the rule that fired each action;
splitting them gives you a clearer trace.

### Prefer `require_approval` over `deny` initially

`deny` is final — the model can't proceed. `require_approval` puts
the human in the loop. Start with `require_approval` and only
promote to `deny` after you've seen the rule fire correctly for a
while.

## State + chaining

Working memory persists across rule iterations within one tool-call
evaluation, but resets between calls. To carry state across calls,
the engine integrates with the SDK's middleware state:

```yaml
- name: count_writes
  when:
    - tool_call:
        name: { in: [write_file, edit_file] }
  then:
    - assert: write_count_increment
```

Then a second rule:

```yaml
- name: cap_writes_per_session
  when:
    - write_count: { gt: 100 }
  then:
    - require_approval:
        gate: "Over 100 writes this session — continue?"
        risk: high
```

The state lives in `ExpertRulesState` (per-thread, checkpointed).

## Dreamscape integration

The dreamscape proposer is a background subsystem that reads recent
sessions, identifies repeated patterns, and proposes new rules. Run
it manually:

```text
/expert watch start 3600 --apply
```

Every hour, the proposer scans the last 24h of sessions and drafts
rules for any pattern that's repeated at least 5 times. With
`--apply`, accepted proposals land in
`.bog-agents/expert_rules/proposed/<timestamp>.yaml` — review them
before promoting.

Without `--apply`, proposals queue in `/expert proposals` for manual
review. Pair this with `/expert wizard` for a conversational
"refine this draft" loop.

See [docs/advanced/dreamscape.md](dreamscape.md) for the full
proposer story.

## Performance

The engine runs O(P × F^k) where P is rules, F is facts, k is
pattern arity. At the project's default scale (~50 rules, ~20
facts per evaluation, k=2-3), one evaluation completes in
microseconds. The slow-run warning fires at 50ms by default
(`BOG_AGENTS_RULES_SLOW_WARN_MS=0` disables, set to a lower number
for CI sensitivity).

If your rulebook grows past 200 rules, watch the trace for
unnecessary `assert` chains — those multiply iteration count.

## Common patterns

### Pattern: cost guardrail

```yaml
- name: cost_brake
  description: Surface approval past $5 per session.
  salience: 80
  when:
    - session:
        cost_usd: { gt: 5.0 }
  then:
    - require_approval:
        gate: "Session cost exceeded $5.00 — continue?"
        risk: high
```

### Pattern: pre-flight check before destructive ops

```yaml
- name: pre_delete_audit
  description: Audit-log every delete-file call.
  salience: 50
  when:
    - tool_call:
        name: delete_file
  then:
    - audit_log:
        event: file_delete_attempted
        metadata:
          path: "${tool_call.args.path}"
    - require_approval:
        gate: "Delete ${tool_call.args.path}?"
        risk: medium
```

### Pattern: route to a careful sub-agent

```yaml
- name: route_security_to_careful
  description: Security tasks go through the careful sub-agent.
  salience: 90
  when:
    - tool_call:
        name: task
        prompt:
          matches: "(?i)security|vulnerability|exploit|auth"
  then:
    - route: careful_security_agent
```

### Pattern: deny in plan mode

```yaml
- name: deny_mutations_in_plan_mode
  description: Plan mode should never mutate the filesystem.
  salience: 100
  when:
    - session:
        plan_mode: true
    - tool_call:
        name: { in: [write_file, edit_file, multi_edit_file, shell_execute] }
  then:
    - deny: "Plan mode is read-only. Toggle plan mode off to make changes."
```

## Filing a bug

If the engine behaves unexpectedly:

1. `/why <call>` — what fired and why.
2. `/expert lint` — confirm your rules parse cleanly.
3. Attach the rule file and the trace from `/why` to a bug report.

The engine is exhaustively tested at
`tests/unit_tests/expert_engine/` and the trace format is stable
across releases.

## Next steps

- [Dreamscape](dreamscape.md) — the proposer that drafts new rules
- [TraceFile v1](tracefile.md) — open trace format for trace export
- [Compliance](compliance.md) — `/compliance` sealed reports built
  on the rule engine

---

*Rules don't replace judgment. They preserve the judgment you've
already made.*
