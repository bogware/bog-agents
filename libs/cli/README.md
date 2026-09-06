# Bog Agents CLI

> *Pass through in harmony. Opinionated where it matters.*

A coding agent that lives in your terminal. Point it at the work, step back,
let it run — and trust the result even when you weren't watching.

No scaffolding. No config ceremony. One install and you've got file access,
a shell, git, code review, planning, sub-agents — the whole outfit. Works
with any LLM that does tool calls: Anthropic, OpenAI, Bedrock, Google,
Ollama, and a dozen others.

Built on the [Bog Agents SDK](https://github.com/bogware/bog-agents) and
[LangGraph](https://github.com/langchain-ai/langgraph). MIT.

[![PyPI](https://img.shields.io/pypi/v/bog-agents-cli)](https://pypi.org/project/bog-agents-cli/)
[![Python](https://img.shields.io/pypi/pyversions/bog-agents-cli)](https://pypi.org/project/bog-agents-cli/)
[![License](https://img.shields.io/pypi/l/bog-agents-cli)](https://opensource.org/licenses/MIT)
[![Downloads](https://img.shields.io/pepy/dt/bog-agents-cli)](https://pypistats.org/packages/bog-agents-cli)

---

## Why bog-agents-cli

You can have an agent in your terminal in under a minute. You can also build
one out from there for years. The CLI is shaped to support both.

- **Patient by default.** Provider hiccups retry. Hung commands time out. A
  crash drops a redacted panic dump so a bug report writes itself.
- **Secure-by-default.** Filesystem confined to the project root unless you
  explicitly opt out; a `--restricted` profile a process can't escape; secrets
  in an in-memory vault that's never persisted; OAuth tokens written atomically
  at `0o600` mode before the rename.
- **Governed autonomy.** `/team run`, `/best-of-n`, and `/jury` turn the engine
  up — bounded by hard cost caps, packaged with proof-of-work evidence.
- **Discoverable.** Type `/` and a fuzzy menu shows you 140+ commands. The
  MCP marketplace browses 35+ servers across 9 categories. `--doctor-deep`
  probes every external dependency in under a second.
- **Drivable by machines.** One-shot prompts, `--json` / `--jsonl` structured
  output, a headless slash-command surface (`bog-agents command`), `--drive`
  for scripting the whole TUI, and `mcp-server` so another agent can delegate to
  it. Built so an AI agent or CI job can operate the CLI end to end.
- **A *bog* aesthetic.** Matte swamp palette — muted moss, lichen-grey,
  firefly-amber warnings, heather-rust errors. A still pool, not a neon arcade.

---

## Install

```bash
# pipx is recommended (isolates dependencies, gives you a clean PATH entry)
pipx install bog-agents-cli

# or with uv
uv tool install bog-agents-cli

# or plain pip
pip install bog-agents-cli
```

You'll need at least one provider API key in the env:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # Claude
export OPENAI_API_KEY=sk-...          # GPT
# or any of: GOOGLE_API_KEY, AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY,
# GROQ_API_KEY, MISTRAL_API_KEY, DEEPSEEK_API_KEY, FIREWORKS_API_KEY, ...
```

Or run local with [Ollama](https://ollama.com/) — no key needed. Verify the
install:

```bash
bog-agents --doctor-deep
```

That probes Python, your config dirs, git, your provider keys, network
reachability, your MCP config, and any recent crash dumps — one-page health
summary in under a second.

---

## 30-second tour

```bash
bog-agents
```

Drops you into a TUI. Type a question, press Enter. The agent has filesystem,
shell, git, and code-edit tools out of the box. Slash-tab auto-completes
commands; `/help` opens the full reference.

Or one-shot it for scripts:

```bash
bog-agents -p "explain what this module does" < src/agent.py
bog-agents -n "fix the failing test in tests/test_auth.py" --auto-approve
```

`-n` is non-interactive (auto-exit), `-p` is pipe-friendly (clean stdout, no chrome).

---

## Killer features

### Governed autonomy — turn the engine up

Bounded by hard cost caps, packaged with proof-of-work evidence, so a run you
didn't watch is a run you can still trust.

```text
/team run split the migration into schema, data, and rollback; do all three
        # a governed team claims dependency-aware tasks off a shared ledger

/best-of-n 5 make tests/test_scheduler.py deterministic
        # 5 attempts in isolated worktrees, rubric-judged, winner kept

/jury                 # N reviewer models vote SHIP / FIX on the current diff
/self-review --fix    # 5 reviewer lenses over your own diff, loop until clean
```

`/operator` puts a cheap judge in front of every prompt and auto-routes it to an
`easy/medium/hard/max` model tier, biased toward intelligence, balance, or cost.

### A findings ledger you can gate CI on

A durable, triageable ledger keyed by a stable fingerprint, so re-scans update
findings instead of duplicating them. SARIF out; CI gate built in.

```text
/findings                                  # open findings, worst first
/findings triage <fp> false_positive "sanitised upstream"
/remediate <fp>                            # a fix turn with the finding's evidence
```

```bash
bog-agents command "/findings gate --max high"   # exit 1 fails the build in CI
bog-agents command "/findings sarif out.sarif"
```

### Agent-authored workflows, saved as slash commands

```text
/workflow author a release-readiness check: map changes, review, test, summarise
/release-readiness v0.10        # run the saved workflow, resumable, under a budget
```

Each phase fans out as a governed team; review and verify phases are gates; a
budget pause resumes where it stopped.

### Plan, review, then execute

```text
/plan                    # toggle plan-mode (think, don't act)
/review-plan             # review a plan line by line — comment, toggle slices, approve or revise
```

```bash
bog-agents --plan "add rate limiting to the API" --auto
        # a read-only planning pass prints the plan, then a second pass executes it
```

### Governance and safety that enforces

```bash
bog-agents --restricted            # no shell/git/raw-HTTP tools; filesystem confined
```

```text
/permissions            # trust posture, workspace trust, and any org-signed managed policy
/actionlog verify       # the hash-chained, signable audit trail of approvals + tool calls
```

Add a signed `managed_policy` document to pin the model gateway, allow-list MCP
servers and skills, forbid plugins, and enforce zero-retention across a fleet.
`.bog-agents/sandbox.toml` wraps every shell command in an OS sandbox with a
network egress allowlist.

### Cost you can see and cap

```text
/cost                   # spend this session, per model / tool, with the daily ledger
/usage                  # per-response token usage as it streams
```

Per-run budgets **pause** at the cap instead of crashing the turn; resume with a
higher cap. `bog-agents --mini` runs the lean harness profile plus deferred tool
schemas for a fraction of the per-turn overhead; `/tokens middleware` shows
exactly what each middleware and tool costs.

---

## Driving it headless

When the operator is another program, you want clean exits and machine-readable
output.

```bash
bog-agents -n "summarize the diff on this branch" --json
```

`--json` wraps the run in a single envelope (final text + tool calls); `--jsonl`
streams one event per line (`start`, `text`, `tool_call`, `tool_result`,
`final`) so a caller can follow the run live.

Informational and configuration slash commands run without booting the TUI —
including the ones that gate CI:

```bash
bog-agents command "/help"                        # full command reference
bog-agents command "/findings gate --max high"    # exit 1 fails the build
bog-agents command "/memory rebuild"              # model-free memory consolidation
bog-agents command "/version" --json
```

Exit codes: `0` success, `1` ran-but-failed, `2` unknown or not-available-headless.

Serve the agent *as* an MCP server so any MCP client (Claude Desktop, Cursor,
Zed, Copilot) can delegate a whole coding task to it:

```bash
bog-agents mcp-server
```

For exercising the *interactive* surface non-interactively — typed prompts,
modal interactions, snapshots, assertions — see the
[`bog-agents --drive` example](#bog-agents-drive-example) below.

---

## Highlights

Organised by what they do rather than which release shipped them; see
[`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)
for the version history.

- **Auto mode + plan mode.** Shift+Tab cycles `default → accept-edits → plan`,
  Ctrl+T toggles bypass, with a live status indicator and a
  `--permission-mode {default,acceptEdits,plan,bypass,paranoid}` flag. Headless
  `--plan "…" [--auto]` plans read-only, then executes.
- **Review surfaces.** `/review` (structured), `/self-review` (five-lens
  pre-submit gate, `--fix` loops), `/jury` (multi-model vote on a diff),
  `/review-plan` (line-addressed plan review), `/ci-fix` (read the branch's CI via
  `gh`, diagnose and fix).
- **Governed autonomy.** `/team run`, `/best-of-n`, `/operator`, `/workflow`,
  all under `RunawayCaps`, with `/tasks` as the one task command center and
  `/cost` + `/usage` for spend certainty.
- **Findings & remediation.** `/findings` ledger with SARIF and a CI gate,
  `/remediate <fp>` → fix turn, a packaged `security-scan` recipe.
- **Governance.** `--restricted` trust profiles, signed managed policy, the OS
  sandbox with an egress allowlist, the Claude-Code/Cursor-compatible hook bus,
  and the hash-chained `/actionlog`.
- **Memory & context.** `@codebase` semantic search, repo
  `.bog-agents/prompts/*.prompt.md` that auto-register as slash commands, a
  `remember` tool for auto-memories, `/memory rebuild` to consolidate them, and
  `!command` shell pass-through into the agent's context.
- **Sub-agents.** `/subtask` and `/fork` (a fork inherits the whole conversation),
  `/agent` to spawn and list.

## Flagship capabilities, carried forward

### `/peat` — your personal assistant

A long-lived in-process sub-agent with a hand-crafted persona. Schedules
recurring jobs (cron, `@every`, `@once`), runs deep research with a five-phase
plan, builds personalized digests.

```bash
/peat schedule "0 9 * * 1-5 | summarize yesterday's QA results"
/peat research "vector databases" --focus pricing,perf
```

### `/qa` — adaptive QA harness

Acceptance-criteria-driven QA plans. Ingest from Jira (via your MCP Jira tool),
file, JSON, or stdin. Steps are agent / shell / http / mcp; verdicts use
`exit_code`, `status`, `contains`, `regex`, or `json_path`.

```bash
/qa new --from-jira PROJ-134
/qa run <plan_id> --var base_url=https://staging.example.com
```

### `/record` + `/replay` — sessions you can edit and re-run

`/record start` captures prompts, responses, and tool calls; `/record stop`
finalizes to a YAML file with auto-detected variables replaced by `${var}`
placeholders; `/replay run` prompts for any unfilled variables and dispatches.

### MCP marketplace

35+ curated servers across 9 categories — github, jira, gitlab, slack, postgres,
mongodb, bigquery, snowflake, aws, notion, kubernetes, sentry, and more.

```bash
/mcp marketplace          # browse the catalog
/mcp install jira         # install from the catalog
/mcp add my-tool ...      # custom server
```

---

## Day-to-day commands

| | |
|---|---|
| `/help` · `/model` · `/effort` | Reference; switch models; the reasoning knob |
| `/operator` | Auto-route each prompt to an easy/medium/hard/max tier |
| `/plan` · `/review-plan` | Plan-mode; review a plan line by line before it runs |
| `/review` · `/self-review` · `/jury` | Structured review; five-lens gate; jury vote on a diff |
| `/team` · `/best-of-n` | Governed team over a task ledger; N judged worktree attempts |
| `/workflow` · `/<name>` | Author and run agent-authored multi-phase workflows |
| `/findings` · `/remediate` | The findings ledger; turn a finding into a fix |
| `/cost` · `/usage` · `/changes` | Spend; per-response usage; the turn-end changes tray |
| `/permissions` · `/actionlog` | Trust posture + org policy; the hash-chained audit trail |
| `/memory` · `/threads` · `/recap` | Consolidate memory; browse threads; where this session stands |
| `/tasks` · `/subtask` · `/fork` | Task command center; background sub-tasks; fork the conversation |
| `/add-dir` | Mount another directory at `/mnt/<name>/` for multi-repo work |
| `/mcp` · `/skills` · `/plugin` | MCP marketplace + manager; skills; agent plugins |
| `/bedrock` · `/compliance` · `/audit` | Bedrock status/fix; compliance auditor; dependency audit |
| `/peat` · `/qa` · `/record` · `/replay` | Personal assistant; QA harness; capture and re-run |

140+ commands total. Type `/` and start typing — fuzzy autocomplete surfaces
what you need. Full grouped reference:
[`docs/cli/commands.md`](https://github.com/bogware/bog-agents/blob/main/docs/cli/commands.md).

---

## Working with sandboxes

Run the agent inside an isolated sandbox instead of directly on your host:

```bash
bog-agents --sandbox docker      # local Docker container
bog-agents --sandbox daytona     # remote daytona.io workspace
```

`docker` and `daytona` are the ready paths; `--sandbox {modal,runloop,langsmith}`
are accepted for their respective providers when that extra is installed. The
first-party sandbox shipped as source today is **Daytona**
(`libs/partners/daytona/`). Separately, `.bog-agents/sandbox.toml` drives the
SDK's **OS-level** sandbox (bubblewrap / seatbelt + an egress allowlist) around
the local shell — see [Governance & safety](https://github.com/bogware/bog-agents/blob/main/docs/cli/governance.md).

---

## Headless modes at a glance

| Flag / subcommand | Use when |
|---|---|
| `-n MSG` | Run a task and exit. Great for CI / scripts. |
| `-p MSG` | Same as `-n` but quiet — clean stdout for pipes. |
| `--plan MSG [--auto]` | Plan read-only, then (with `--auto`) execute the plan. |
| `--json` / `--jsonl` | Single JSON envelope / one event per line. |
| `command "/…"` | Run a headless-capable slash command (`/findings gate`, `/memory rebuild`, `/help`, `/version`, …). |
| `mcp-server` | Serve the agent over MCP so another client can delegate to it. |
| `--restricted` / `--mini` | Locked-down tool set / low-overhead harness (compose with any of the above). |
| `--prompt NAME` / `--pipeline NAME` | Run a saved prompt / pipeline. |
| `--drive PATH` | Run a YAML drive script that emulates a TUI user (Pilot-based, JSONL out). |
| `--serve` / `--acp` | Long-running HTTP server / Agent Client Protocol (Zed). |

### `bog-agents drive` example

```yaml
# smoke.yaml
session:
  model: fake:Hello from drive.
  approval_mode: auto-all
steps:
  - "/help"
  - wait_for_idle: 5
  - expect_transcript_contains: "(?i)help|usage"
  - type: "summarize the README"
  - submit
  - wait_for_idle: 30
  - snapshot: artifacts/after-summary
```

```bash
bog-agents --drive smoke.yaml
# stdout (one JSONL row per step + summary):
# {"step":0,"action":"slash","ok":true,"duration_ms":42,...}
# {"summary":{"total":6,"passed":6,"failed":0,"duration_ms":3210}}
```

Exit code = number of failed steps. The runner produces SVG + text snapshots for
visual review and a JSONL transcript for diffing.

---

## Configuration

Settings cascade — later layers override earlier: built-in defaults →
`~/.bog-agents/` (user global) → `<project>/.bog-agents/` (project-local). Scalar
knobs are centralised in the config manifest (`config.toml`, env var, or default;
precedence env > toml > default). Every section is optional; you can ship without
any settings file at all.

---

## Always-fresh local development

```bash
git clone https://github.com/bogware/bog-agents
cd bog-agents/libs/cli
uv sync --reinstall
uv run bog-agents
```

`uv sync --reinstall` rebuilds every editable package from source.

---

## Documentation

- This README + `bog-agents --help`
- **Full docs**: <https://github.com/bogware/bog-agents/tree/main/docs>
  — [getting started](https://github.com/bogware/bog-agents/blob/main/docs/getting-started.md),
  [command reference](https://github.com/bogware/bog-agents/blob/main/docs/cli/commands.md),
  [governed autonomy](https://github.com/bogware/bog-agents/blob/main/docs/cli/governed-autonomy.md),
  [findings & security](https://github.com/bogware/bog-agents/blob/main/docs/cli/findings.md),
  [governance & safety](https://github.com/bogware/bog-agents/blob/main/docs/cli/governance.md),
  [drive deep dive](https://github.com/bogware/bog-agents/blob/main/docs/cli/drive.md)
- Architecture overview: [`CLAUDE.md`](https://github.com/bogware/bog-agents/blob/main/CLAUDE.md)
- Repo: <https://github.com/bogware/bog-agents> · Issues:
  <https://github.com/bogware/bog-agents/issues> ·
  Changelog: [`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)

---

## License

MIT. See [LICENSE](https://github.com/bogware/bog-agents/blob/main/LICENSE).

*Pass through in harmony.*
