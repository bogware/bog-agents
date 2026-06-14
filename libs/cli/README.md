# Bog Agents CLI

> *Pass through in harmony. Opinionated where it matters.*

A coding agent that lives in your terminal. Point it at the work, step back,
let it run.

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

- **Patient by default.** Provider hiccups retry. Hung commands time out.
  Mouse-tracking escape sequences don't leak into your input box. A crash
  drops a redacted panic dump so a bug report writes itself.
- **Secure-by-default.** Filesystem confined to the project root unless you
  explicitly opt out. Secrets live in an in-memory vault that's never
  persisted to disk. OAuth tokens written atomically at `0o600` mode set
  before the rename — no world-readable race window.
- **Discoverable.** Type `/` and a fuzzy menu shows you 120+ commands. The
  MCP marketplace browses 35+ servers across 9 categories. `--doctor-deep`
  probes every external dependency in under a second.
- **Drivable by machines.** Run it without a human at the keyboard:
  one-shot prompts, `--json` / `--jsonl` structured output, a headless
  slash-command surface (`bog-agents command`), and `bog-agents drive`
  for scripting the whole TUI. Built so an AI agent or CI job can operate
  the CLI end to end.
- **A *bog* aesthetic.** Matte swamp palette — muted moss, lichen-grey,
  firefly-amber warnings, heather-rust errors. Ultima-inspired heavy-serif
  splash banner with rune anchors. A still pool, not a neon arcade.

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

Or run local with [Ollama](https://ollama.com/) — no key needed.

Verify the install:

```bash
bog-agents --doctor-deep
```

That probes Python, your config dirs, git, your provider keys, network reachability,
your MCP config, and any recent crash dumps — one-page health summary in under a second.

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

## Driving it headless

When the operator is another program, you want clean exits and machine-readable
output. The CLI gives you three levels of that.

### One-shot agent runs

```bash
bog-agents -n "summarize the diff on this branch" --json
```

`--json` wraps the run in a single envelope (final text + tool calls);
`--jsonl` streams one event per line (`start`, `text`, `tool_call`,
`tool_result`, `final`) so a caller can follow the run live.

### Headless slash commands

Informational and configuration slash commands run without booting the TUI:

```bash
bog-agents command "/help"          # full command reference
bog-agents command "/commands"      # list commands; * marks headless-capable
bog-agents command "/version" --json
bog-agents command "/model"         # show the configured model
bog-agents command "/config"        # resolved config + file path
```

Exit codes: `0` success, `1` ran-but-failed, `2` unknown or not-available-headless.
Commands that are inherently interactive return a clear message listing the ones that
aren't — no silent hangs.

### Scripted TUI (`bog-agents drive`)

For exercising the *interactive* surface non-interactively — typed prompts,
modal interactions, snapshots, assertions — see [Drive](#bog-agents-drive-example) below.

---

## What's new in 0.9.x

- **0.10 — Claude-Code-style auto mode + self-verification.**
  - **Permission modes:** Shift+Tab cycles `default → accept-edits → plan`,
    Ctrl+T toggles bypass, with a live status indicator and a
    `--permission-mode {default,acceptEdits,plan,bypass,paranoid}` flag (and
    `--dangerously-skip-permissions`).
  - **`bog-agents mcp-server`** — serve the agent over MCP so any MCP client
    (Claude Desktop, Cursor, Zed, Copilot) can delegate a coding task to it.
  - **`/self-review`** (five reviewer lenses over your own diff → SHIP/FIX-FIRST,
    `--fix` to loop) and **`/ci-fix`** (read the branch's CI via `gh`, diagnose
    and fix failures).
  - **`@codebase`** semantic search, **repo `.bog-agents/prompts/*.prompt.md`**
    that auto-register as slash commands, **auto-memories** (a `remember` tool),
    and **shell pass-through** — `!command` output now enters the agent's context.
- **0.9.4 — headless driving + provider resilience.** A headless
  slash-command surface (`bog-agents command "/help"`), `--jsonl`
  structured streaming with tool-call events, and deepagents parity
  carried up from the SDK. Live-tested across Anthropic, AWS Bedrock,
  and OpenAI.
- **0.9.1 — Bedrock, seamless.** Automatic inference-profile resolution,
  `/bedrock fix` + `/bedrock config`, auto SSO-credential refresh. Point
  at a model id and ride.
- **0.9.0 — scriptable TUI, compliance, security sweep.** `bog-agents drive`
  graduated to a full Pilot-backed runner; `/compliance` auditor with
  HMAC-sealed reports; a repo-wide security pass.

See [CHANGELOG.md](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)
for the full history. The flagship 0.8.0 capabilities below are all still here.

## Flagship capabilities (0.8.0, carried forward)

### `/peat` — your personal assistant

A long-lived in-process sub-agent with a hand-crafted persona. Schedules
recurring jobs (cron, `@every`, `@once`), runs deep research with a
five-phase plan, builds personalized digests from your `/qa` results and
`/replay` recordings.

```bash
/peat schedule "0 9 * * 1-5 | summarize yesterday's QA results"
/peat research "vector databases" --focus pricing,perf
/peat digest --days 7
/peat metrics                # in-process counters this session
/peat config show            # persona, goals, restrictions
```

Hybrid tool surface — full agent powers when you're chatting interactively,
restricted (no shell, write-only into `peat/`) when running unattended.

### `/qa` — adaptive QA harness

Acceptance-criteria-driven QA plans. Ingest from Jira (via your MCP Jira
tool), file, JSON, or stdin. Plans are typed YAML; steps are
agent / shell / http / mcp; verdicts use `exit_code`, `status`, `contains`,
`regex`, or `json_path`. Outputs as Markdown, JSON, stdout, or Jira comment.

```bash
/qa new --from-jira PROJ-134
/qa run <plan_id> --var base_url=https://staging.example.com
/qa show <plan_id>
```

Plans live at `<project>/.bog-agents/qa-plans/`. Hand-edit the YAML to refine.

### `/record` + `/replay` — sessions you can edit and re-run

`/record start` captures user prompts, AI responses, and tool calls live.
`/record stop` finalizes to a YAML file with auto-detected variables (Jira
IDs, repo URLs, file paths) replaced by `${var}` placeholders. `/replay run`
prompts for any unfilled variables — secrets via masked input that route
to the in-memory vault — then dispatches to the agent.

```bash
/record start  fix-login-bug
… use the agent normally …
/record stop                                                  # YAML written
# edit ~/.bog-agents/replays/<id>.yaml — refine var names, mark secrets
/replay run fix-login-bug --var jira_ticket=JIRA-456
```

### Vault + Vars

A typed variable system shared by `/replay` and `/qa`. `string`, `secret`,
`enum`, `int`, `bool`. Secrets live only in process memory; nothing
persists to disk. Optional read-only OS-keychain bridge via the
`keyring` library.

### MCP marketplace, expanded

```bash
/mcp marketplace
```

35+ curated servers across 9 categories: github, jira, gitlab, slack,
postgres, mongodb, redis, bigquery, snowflake, supabase, aws, azure-devops,
terraform, cloudflare, stripe, hubspot, notion, confluence, google-drive,
discord, kubernetes, datadog, sentry — and more.

```bash
/mcp install jira
/mcp add my-tool /usr/local/bin/my-tool --flag value   # custom server
/mcp                                                    # live viewer
```

---

## Day-to-day commands

| | |
|---|---|
| `/help` | Full command reference |
| `/model` | Switch model on the fly |
| `/bedrock` | Bedrock inference-profile status, `fix`, `config` |
| `/profile` | Apply a saved configuration preset |
| `/plan` | Toggle plan-mode (think-then-act) |
| `/effort` | Adjust thinking budget for the current model |
| `/compact` | Summarize and prune the current thread |
| `/resume` | Pick a recent thread to continue from |
| `/threads` | Browse all threads |
| `/diff` | Show changes since the agent started |
| `/agent` | Spawn / list sub-agents |
| `/review` | Structured code review on the current diff |
| `/jury` | Run the diff past N juror models, aggregate verdicts |
| `/race` | Run the same prompt past multiple models in parallel |
| `/audit` | Dependency vulnerability audit |
| `/test` | Test generation, coverage, audit |
| `/build` | Pipeline / recipe builder |
| `/compliance` | Compliance auditor with HMAC-sealed reports |
| `/peat` | Personal assistant + scheduler (see above) |
| `/qa` | Acceptance-criteria QA harness (see above) |
| `/record` / `/replay` | Capture and re-run sessions (see above) |
| `/mcp` | MCP server marketplace + manager |
| `/quit` | Exit |

120+ commands total. Type `/` and start typing — fuzzy autocomplete will surface
what you need.

---

## Built-in observability

- **Structured event logs** at every chokepoint: agent run start/end, tool
  dispatch, scheduler fires, vault reads, provider calls. Stable event
  names, prefixed `evt_*` fields — drop straight into Splunk, Loki, or
  `grep`.
- **`/peat metrics`** — in-process counter snapshot for the current session.
- **`--doctor-deep`** — runtime probes of every external dependency.
- **Panic dumps** — uncaught exceptions land at `~/.bog-agents/crash/<ts>.log`
  with redacted host info, versions, traceback, and recent metrics. Attach
  the file when you open an issue.

---

## Configuration

Settings cascade — later layers override earlier:

1. Built-in defaults
2. `~/.bog-agents/settings.json` — user global
3. `<project>/.bog-agents/settings.json` — project-local

Knobs include the auto-mode rule engine, Peat persona, MCP trust list,
hooks, profiles, keybindings, and more. Every section is optional; you can
ship without any settings file at all.

---

## Working with sandboxes

Run the agent inside an isolated remote sandbox instead of on your host:

```bash
bog-agents --sandbox docker      # local Docker container
bog-agents --sandbox daytona     # remote daytona.io workspace
bog-agents --sandbox modal       # Modal sandbox (when the extra is installed)
bog-agents --sandbox runloop     # RunLoop sandbox (when installed)
bog-agents --sandbox langsmith   # LangSmith hosted sandbox
```

The first-party sandbox shipped as source today is **Daytona**
(`libs/partners/daytona/`). The other providers are configurable via their
respective extras; see the SDK docs for credentials and limits.

---

## Headless modes at a glance

| Flag / subcommand | Use when |
|---|---|
| `-n MSG` | Run a task and exit. Great for CI / scripts. |
| `-p MSG` | Same as `-n` but quiet — clean stdout for pipes. |
| `--json` | Emit the result as a single JSON envelope (text + tool calls). |
| `--jsonl` | Stream one JSON event per line (start / text / tool_call / tool_result / final). |
| `command "/…"` | Run a headless-capable slash command (`/help`, `/version`, `/model`, `/config`, `/commands`, `/changelog`). |
| `--prompt NAME` | Run a saved prompt from your prompt library. |
| `--prompt-vars JSON` | Pass variable bindings to a saved prompt. |
| `--pipeline NAME` | Run a saved pipeline from `.bog-agents/pipelines/`. |
| `--drive PATH` | Run a YAML drive script that emulates a TUI user (Pilot-based, JSONL out). |
| `--drive-stdin` | Read the drive script from stdin instead of a file. |
| `--serve` | Long-running HTTP server mode. |
| `--acp` | Agent Client Protocol mode (Zed editor). |

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
# {"step":1,"action":"waitforidle","ok":true,...}
# {"summary":{"total":6,"passed":6,"failed":0,"duration_ms":3210}}
```

Exit code = number of failed steps. The runner produces SVG + text
snapshots for visual review and a JSONL transcript for diffing.

---

## Always-fresh local development

```bash
git clone https://github.com/bogware/bog-agents
cd bog-agents/libs/cli
uv sync --reinstall
uv run bog-agents
```

`uv sync --reinstall` rebuilds every editable package from source. Add
`--no-cache` to a single `uv run` to bypass the resolver cache for one shot.

---

## Documentation

- This README + `bog-agents --help`
- **Full docs**: <https://github.com/bogware/bog-agents/tree/main/docs>
  — [getting started](https://github.com/bogware/bog-agents/blob/main/docs/getting-started.md),
  [cookbook](https://github.com/bogware/bog-agents/blob/main/docs/cookbook.md),
  [troubleshooting](https://github.com/bogware/bog-agents/blob/main/docs/troubleshooting.md),
  [drive deep dive](https://github.com/bogware/bog-agents/blob/main/docs/cli/drive.md),
  [tips & tricks](https://github.com/bogware/bog-agents/blob/main/docs/tips-and-tricks.md)
- Architecture overview: [`CLAUDE.md`](https://github.com/bogware/bog-agents/blob/main/CLAUDE.md)
- Repo: <https://github.com/bogware/bog-agents>
- Issues: <https://github.com/bogware/bog-agents/issues>
- Changelog: [`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)

---

## License

MIT. See [LICENSE](https://github.com/bogware/bog-agents/blob/main/LICENSE).

*Pass through in harmony.*
