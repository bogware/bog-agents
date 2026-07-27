# Tips & Tricks

> Power-user moves the rest of the docs don't surface elsewhere. A
> grab bag. Read what looks relevant, skip the rest.

## TUI moves you'll wish you knew sooner

### Tab-complete slash commands

Type `/` and start typing. The fuzzy menu does substring + acronym
matching. `/exp` finds `/expert`. `/pl` finds `/plan`. `/orch f`
finds `/orchestrate --fast`.

### Switch model mid-session

```text
/model
```

Drops you into a picker. Filter by typing. Anthropic models with
"sonnet" in the name? `son`. The picker shows the resolved context
window and provider next to each entry so you can compare before
committing.

### Cancel the agent without quitting

`Escape` interrupts the current turn. The agent stops, the partial
tool-call state is rolled back, your next prompt starts fresh on
the same thread.

`Ctrl+C` once does the same. `Ctrl+C` twice quits.

### See every file the agent touched

```text
/diff
```

Shows the working-tree diff since the agent started. Useful for
"wait, what did it actually change?" moments. `/diff --staged` if
you've been pre-staging.

### Free up context without losing the thread

```text
/compact
```

Summarizes older turns into a SystemMessage, drops the literal
text. Free for a few hundred turns of fresh capacity. The summary
preserves the gist; the literal turns are gone.

`/compact --aggressive` keeps only the last 5 turns + summary.

### Switch threads

```text
/threads
```

Lists every recent thread with last-modified time and a one-line
summary. Pick one to resume. Same `thread_id` = same conversation
history.

`/threads --keyword secrets-test` filters.

### Plan mode

```text
/plan
```

Toggles plan mode. The agent must produce a plan before executing.
Tool calls that mutate state are gated until you approve the plan.
Use for high-stakes work where you want to see the strategy before
the strategy runs.

### Adjust effort

```text
/effort high
```

Increases the model's thinking budget. Costs more, takes longer,
gets better answers. `/effort low` is the cheap-fast option.
Default is `medium`.

For Claude, this maps to extended thinking tokens. For other models
the mapping is provider-specific.

## Shell-fu

### Pipe context in

```bash
git log --since="1 week ago" --oneline | bog-agents -p "summarize what's new"
cat error.log                          | bog-agents -p "what's the root cause?"
gh pr view 42 --json files,body        | bog-agents -p "anything risky in this PR?"
curl -s api.example.com/status         | bog-agents -p "is anything wrong here?"
```

### One-shot scripted agent

```bash
bog-agents -n "fix all the typos in README.md" --auto-approve --no-stream
```

`-n` exits when the agent finishes. `--auto-approve` skips per-tool
prompts. `--no-stream` buffers stdout and flushes at the end (good
for shell pipelines that don't tolerate streaming).

### Quiet mode for clean output

```bash
result=$(bog-agents -p "extract every TODO from src/" | sort -u)
echo "$result" > todos.txt
```

`-p` (quiet) sends everything except the agent's response text to
stderr. Stdout is pipeable directly.

### Use a saved prompt with vars

```bash
bog-agents --prompt weekly-summary --prompt-vars '{"repo": "myorg/myrepo", "days": 7}'
```

Prompts live in `~/.bog-agents/prompt_library.toml`. Variables use
`{{name}}` (double curlies) inside the body.

### Multi-step pipelines

```yaml
# .bog-agents/pipelines/release.yaml
description: Bump version, write changelog, tag, push
steps:
  - id: bump
    type: message
    text: Bump the version in pyproject.toml. Use the next semver.

  - id: changelog
    type: message
    text: Update CHANGELOG.md with the new version's entries.

  - id: tag
    type: shell
    text: |
      git add -A
      git commit -m "release: $(grep '^version' pyproject.toml | head -1)"
      git tag "v$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)"
```

```bash
bog-agents --pipeline release
```

Inlines as a multi-step prompt; the agent treats each step as a
subtask and reports on every one.

## SDK shortcuts

### Drop the system prompt addendum

```python
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    system_prompt="...",
    # Skip the BASE_AGENT_PROMPT prepend
    skip_base_prompt=True,
)
```

You take responsibility for the agent knowing how to behave. Useful
for highly-specialized agents.

### Use FakeChatModel for tests

```python
from bog_agents import create_agent
from bog_agents_cli.drive.replay_model import FakeChatModel

agent = create_agent(
    model=FakeChatModel(response_text="ok"),
)
```

No network, no API key, deterministic. Great for unit-testing your
own middleware.

### Inspect a graph's middleware list

```python
import bog_agents.graph as g

# Monkey-patch _validate_middleware_ordering to capture
captured = []
orig = g._validate_middleware_ordering
g._validate_middleware_ordering = lambda lst: (captured.extend(lst), orig(lst))[1]

agent = create_agent(model=..., config=FeatureConfig(enable_git_tools=True))

for m in captured:
    print(type(m).__name__)
```

That's how `test_middleware_canonical_order.py` works. Use it to
debug "did my middleware land in the right position?"

## Daemon shortcuts

### Hot-reload jobs

The daemon reloads jobs from `~/.bog-agents/daemon/jobs/*.yaml` on
each scheduler tick. Edit a job file; the next tick picks up the
change. No restart needed.

### Trigger from anywhere

```bash
# From your laptop
ssh prod-server "curl -X POST -H 'Authorization: Bearer $TOKEN' \
  http://localhost:7878/api/jobs/morning-brief/run"
```

The daemon's REST API is the universal trigger. Any tool that can
HTTP can fire a job.

### Build a Slack slash command

Point Slack's slash command at:

```
https://your-daemon.example.com/webhook/slack-command
```

With a job that has `--webhook slack-command` and a prompt that
references `{trigger_context.text}` (Slack puts the user's command
text in the webhook payload).

## Observability tricks

### `--doctor-deep` is the always-correct status check

```bash
bog-agents --doctor-deep
```

Probes Python, config dirs, git, provider keys, network reachability,
MCP config, recent crash dumps. One-page health summary in under a
second. Run before filing any bug.

### Tail the structured event log

```bash
tail -f ~/.bog-agents/logs/cli.log | grep -E 'evt_(tool|model|approval|error)'
```

The `evt_*` prefix is stable across releases. Drop into Splunk /
Loki / journald for production.

### Capture a stuck session

When the agent's hung but the TUI's still responsive:

```text
/dump
```

Writes a structured snapshot of current state to
`~/.bog-agents/crash/<ts>.dump.json`: messages, pending tool calls,
middleware state, env summary. Attach to a bug report.

`/dump --without-secrets` is the default. There's no other mode.

## Cost control

### Set a session budget

```bash
bog-agents -n "..." --budget 2.50
```

Past $2.50 in API costs, the agent surfaces an approval for every
subsequent model call. Combine with `--auto-approve` for
"surface only on cost":

```bash
bog-agents -n "..." --budget 2.50 --auto-approve
```

### Use the cheaper model for boring work

```python
agent = create_agent(
    model="anthropic:claude-opus-4-7",
    subagents=[
        SubAgent(
            name="researcher",
            description="Read files and summarize",
            system_prompt="You read and summarize. No edits.",
            model="anthropic:claude-haiku-4-5",  # 5x cheaper
        ),
    ],
)
```

The main agent gets a `task()` tool. When it delegates summarization
work to `researcher`, the cheaper model handles it. The expensive
model only runs for the orchestrating + writing phases.

### Prompt cache aggressively

Anthropic prompt caching is on by default for Claude models. The
cache key is computed from the message list — so if your system
prompt + first turn is stable across runs, you pay 90% less for
those tokens on subsequent runs.

To maximize hits:

- Keep your system prompt stable. Don't add timestamps.
- Pin your AGENTS.md content (don't auto-regenerate it).
- Reuse threads (`/resume`) when the conversation is similar.

### Watch your spend

```text
/cost
```

Shows session-to-date cost, broken down by model + tool. `/cost
reset` clears for a fresh budget window.

For long-term tracking, `enable_cost_tracking=True` +
`AuditTrailMiddleware` write per-call records you can sum offline.

## Speed tricks

### Skip MCP loading

```bash
bog-agents --no-mcp
```

When you don't need any MCP tools, skipping MCP startup saves the
15s wait (worst case) + the per-server probe time. Worth it for
short-lived `-p` invocations.

### Smaller-context model for short tasks

For a task you know fits in 50K tokens, use `claude-haiku-4-5`
instead of `claude-opus-4-7`. Faster first token, lower cost,
fine for the task. Switch back with `/model` when you hit
something hard.

### Pre-warm the model server

```bash
bog-agents --serve &
sleep 5
# Now `bog-agents -p "..."` invocations connect to the warm server
```

The server starts the model once. Subsequent CLI invocations talk
to it via HTTP. Per-invocation startup drops from ~3s to ~200ms.

## Governed autonomy — turn the engine up

These are the moves that separate "chatbot in a terminal" from "an agent you
trust to work while you're asleep." Each one is deterministic and composes with
the cost caps below — no runaway spend.

### Run a whole team on one prompt

```text
/team run --chain design the schema | write the migration | add tests
```

Spins up a governed **agent team**: each task is a claimable item on a shared
ledger, each teammate is a non-interactive agent working in the same repo.
`--chain` makes it a pipeline (task N waits for N-1); drop it and independent
tasks run as workers free up. `--members alice,bob` sets the roster (default
two). Every teammate spawn is counted against a spend cap, so a team can't
fork-bomb your wallet.

### Best-of-N when the first answer isn't good enough

```text
/best-of-n 3 refactor the auth module to remove the global session
```

Runs **N full agent attempts, each in its own git worktree**, then the rubric
grader ranks the resulting diffs and keeps the winner's worktree for you to
inspect. The losers are cleaned up. This is the "I want the *good* version, not
the *first* version" button. Default 3, max 8.

### Vote on a diff before you trust it

```text
/jury
```

A panel of reviewer models each votes on the current `git diff` — approve /
request-changes with reasons. `/jury staged` for staged changes, `/jury <ref>`
against a base. Turns "looks fine to me" into "three independent reviewers
agreed." The juror models come from `[jury].models` in your config; unset =
the active model votes three times.

### Let bog pick the effort for you

```text
/operator on
```

A cheap judge classifies every prompt `easy / medium / hard / max` and, for that
one turn, escalates both the **model** and the **`/effort`** knob — and can route
a genuinely hard job to `butcher` (decompose into slices) or `jtbd` (interview →
job spec → execute). Trivial prompts stay on Haiku; a gnarly refactor gets Opus
at high effort automatically. Judge failures never block a turn — they fall
through to your active model. Presets live in `~/.bog-agents/operator.toml`.

### Proof-of-work on every autonomous change

When an agent finishes an unattended job, the **evidence bundle** packages the
diff stat, the test/verify command output, and the rubric verdict into one
artifact — `merge_ready` is true only when checks pass *and* the rubric is
satisfied. It's what makes "the daemon fixed it overnight" auditable instead of
"trust me." Attached automatically on daemon + serve runs; the pieces are in
`bog_agents.evidence` if you want to build your own.

## Less obvious

### The agent reads AGENTS.md

Drop an `AGENTS.md` in your project root. Anything in it gets
injected as memory at the top of every agent turn. Good content:

- Project-specific conventions ("don't use `requests`; use `httpx`").
- Where things live ("config is in `~/.bog-agents/`, not `./config/`").
- What not to do ("don't run `migrate` on prod without `--dry-run`").

64 KiB cap. Anything past that is silently truncated.

### `bog-agents drive` for any repeatable workflow

The drive runner isn't only for testing. Any time you find yourself
running the same TUI sequence twice, capture it as a script:

```bash
/record start my-workflow
... do the thing ...
/record stop
```

That writes a drive script you can re-run any time:

```bash
bog-agents --drive ~/.bog-agents/replays/<id>.drive.yaml --drive-var ticket=NEW-123
```

See [docs/cli/drive.md](cli/drive.md) for the full grammar.

### The expert rules engine + LLM proposer

```text
/expert watch start 3600 --apply
```

Every hour, the dreamscape proposer reviews recent sessions and
drafts new expert rules. With `--apply`, accepted proposals land in
`.bog-agents/expert_rules/proposed/` for review. Pair with
`/expert wizard` for a conversational "refine this draft" loop.

The proposer learns your preferences over time. Things you keep
denying manually become rule candidates.

### Sandboxes for unsafe operations

If you're about to do something the agent really shouldn't do on
your host:

```bash
bog-agents --sandbox docker
```

The shell runs inside a container. The filesystem is the container's,
not your host. The container dies when the session ends. Use for
"try this risky migration script and tell me what happens" without
risking your real filesystem.

### Native OS sandbox + egress allowlist (Linux/macOS)

No container needed. Drop a `.bog-agents/sandbox.toml`:

```toml
[sandbox]
local_sandbox = "workspace-write"        # read-only | workspace-write | full-access
require_sandbox = true                    # fail closed if no launcher (don't run unconfined)
network_allowlist = ["pypi.org", "github.com"]
```

Now every shell command the agent runs is wrapped in **bubblewrap** (Linux) or
**seatbelt** (macOS): filesystem writes are confined to the working dir, and
network egress is either cut entirely (omit `network_allowlist`) or routed
through a **localhost allowlist proxy** that only lets the listed hosts through
(everything else gets a `403`). `require_sandbox = true` means "refuse to run
rather than run unconfined" — the safe posture on a box without a launcher
(Windows today; native AppContainer is on the roadmap). Honesty note: the
allowlist constrains cooperating tools (pip / git / curl), not a process that
opens a raw socket — use no-allowlist mode for a hard network cut.

### Turn on a completion chime (it's off by default)

A beep on every response gets old fast, so notification sounds ship **off**.
If you *do* want a chime when a long unattended run finishes:

```bash
BOG_AGENTS_SOUNDS=1 bog-agents -n "the long thing" --auto-approve
```

---

*Most of these you'll never need. The few you do, you'll wonder why
the docs didn't lead with them.*
