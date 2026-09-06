<p align="center">
  <strong>The production-grade AI agent framework built on LangGraph.</strong><br>
  Run it in your terminal, embed it in your app, or leave it working on a server —
  one install, a compiled agent, nothing to wire up.
</p>

<p align="center">
  <a href="https://pypi.org/project/bog-agents-cli/"><img alt="PyPI" src="https://img.shields.io/pypi/v/bog-agents-cli?color=1f6feb&label=pypi"></a>
  <a href="https://pypi.org/project/bog-agents-cli/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/bog-agents-cli?color=1f6feb"></a>
  <a href="https://github.com/bogware/bog-agents/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/bogware/bog-agents/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-1f6feb"></a>
</p>

<p align="center">
  <img alt="The bog-agents CLI answering a question in the terminal" src=".github/images/screenshot-tui.svg" width="820">
</p>

Most frameworks hand you the parts and wish you luck. Bog Agents hands you a
working agent — file tools, a real shell, git, sub-agents, plan mode, and ~90
composable middlewares out of the box — then lets you turn the engine up when it
matters: **agent teams**, **best-of-N with a rubric judge**, **agent-authored
workflows**, a **findings ledger you can gate CI on**. Secure-by-default
backends, bounded retries, hard cost caps, and proof-of-work evidence are the
baseline, not an afterthought.

The result is the thing every other framework promises and few deliver: **an
agent you can trust to run when you're not watching.**

Three packages, one stack:

- **[`bog-agents`](libs/bog-agents)** — the Python SDK. `create_agent()` returns a
  compiled LangGraph agent with file tools, a shell, git, sub-agents, plan mode,
  retry-with-backoff, and ~90 composable middlewares. Governed autonomy is
  first-class: **agent teams** (`bog_agents.teams`), **runaway cost caps**
  (`bog_agents.cost_ledger`), **proof-of-work evidence bundles**
  (`bog_agents.evidence`), a **findings ledger** (`bog_agents.findings_store`),
  evals + guardrails as importable primitives, and an **OS-level sandbox**
  (bubblewrap/seatbelt + an egress-allowlist proxy). Drop-in
  [deepagents](https://github.com/langchain-ai/deepagents) compatibility, too.
- **[`bog-agents-cli`](libs/cli)** — a coding agent that lives in your terminal.
  140+ slash commands, any LLM, persistent memory, an MCP marketplace, and a
  full **headless surface** so an AI agent or CI job can drive it without a human
  at the keyboard. Matte-swamp TUI.
- **[`bog-agents-daemon`](libs/daemon)** — the patient watcher. Runs your
  agents on cron / file-change / webhook / git-push triggers; survives
  reboots; scans repositories on a schedule; reports back via Slack / email /
  GitHub / file / webhook.

Built on [LangGraph](https://github.com/langchain-ai/langgraph). MIT-licensed.

---

## Killer features

The parts that put Bog Agents in a class of its own. Every example below is a
real command or a few lines of real code.

### 1. Governed autonomy you can trust unwatched

Turn a single agent into a team, race N attempts and keep the best, or put a
diff to a jury — all bounded by hard cost caps and packaged with proof-of-work
evidence, so a run you didn't watch is a run you can still trust.

```text
/team run implement the OAuth refresh flow; split into API, storage, tests
        # a governed team claims dependency-aware tasks off a shared ledger,
        # coordinates over a mailbox, and stops at the spawn / spend cap

/best-of-n 5 make the flaky scheduler test deterministic
        # five full attempts in isolated git worktrees, each judged against a
        # rubric; the winning worktree is kept, the rest discarded

/jury                      # N reviewer models vote SHIP / FIX on the current diff
```

Every autonomous surface runs under **`RunawayCaps`** (max sub-agents, web
searches, and dollars) and can emit an **evidence bundle** — the diff stat, the
output of your verify command, and the rubric verdict — so "it passed" is a
document, not a claim. `/self-review` is the pre-submit gate: five reviewer
lenses (correctness, security, maintainability, tests, over-claims) over your own
diff, `--fix` to loop until clean.

### 2. A findings ledger you can gate CI on

Scans produce findings as prose; Bog Agents turns them into a durable, triageable
ledger keyed by a **stable fingerprint** (rule + path + normalised message — never
the line number), so a re-scan updates a finding instead of re-opening it, a
fixed issue closes itself, and a `wontfix` stays quiet. Export SARIF for code
scanning, and fail CI on what's still open.

```bash
# In the TUI: run the packaged security-scan recipe, then review the ledger
/findings                          # open findings, worst first
/findings triage <fp> false_positive "sanitised upstream"
/remediate <fp>                    # turn one finding into a fix turn, evidence in hand

# In CI: exit non-zero when anything high or worse is still open
bog-agents command "/findings gate --max high"     # exit 1 fails the build
bog-agents command "/findings sarif out/findings.sarif"
```

The same ledger backs a scheduled daemon `scan` job, so a nightly security or
code-health sweep lands findings your CI reads the next morning.

### 3. Agent-authored workflows, saved as slash commands

Ask the agent to design a repeatable, multi-phase pipeline. It writes the YAML;
Bog Agents validates it, saves it to `.bog-agents/workflows/`, and loads it as a
first-class `/command`. Each phase fans out as a governed team; review and verify
phases are gates; runs persist per-phase so a budget pause resumes where it
stopped.

```text
/workflow author a release-readiness check: map changes, review, run tests, summarise
        # → saved as /release-readiness

/release-readiness v0.10          # run it, resumable, under a hard budget
/workflow status release-readiness
```

### 4. Enterprise-grade governance and safety

Governance that actually enforces, not governance that logs and hopes.

- **`--restricted` trust profiles** strip the shell, git, and raw-HTTP tools and
  confine the filesystem — a profile a process cannot escape, not a prompt it
  might ignore.
- **Signed managed policy** (`managed_policy.py`): one org-signed JSON document,
  verified with a pinned key, that pins the model gateway, allow-lists MCP servers
  and skills, forbids plugins, and enforces zero-retention — surfaced in
  `/permissions` and `/doctor`.
- **OS-level sandbox**: `.bog-agents/sandbox.toml` wraps every shell command in
  bubblewrap (Linux) / seatbelt (macOS) with a hard network cut or a **localhost
  egress-allowlist proxy**; `require_sandbox` fails closed where no launcher exists.
- **Hash-chained action log + OpenTelemetry**: a tamper-evident, signable record
  of every approval and tool call (`/actionlog verify`), plus GenAI-semconv spans
  over dependency-free OTLP/HTTP.
- **Hook bus**: `PreToolUse` / `PostToolUse` / `PreModelSwitch` decisions from
  Claude Code and Cursor hook files, loaded unchanged and hash-pinned.

### 5. Cost certainty

Autonomous runs can't fork-bomb your bill, and you can always see where the money
and the tokens went.

- A **durable spend ledger** with per-run budgets that **pause** at the cap
  (`budget_reached`) instead of crashing the turn — resume with a higher cap.
- **`/operator`** routes each prompt to an `easy/medium/hard/max` model tier with
  a cheap judge, biased toward intelligence, balance, or cost.
- **`/cost`** and **`/usage`** attribute spend per response, per model, per tool.
- **Measured, CI-gated harness overhead.** The SDK's default per-turn overhead is
  ~8,979 tokens before your words (system prompt + tool schemas, approx
  tokenizer); the built-in **`lean`** profile is ~3,115, and the CLI's **`--mini`**
  adds deferred tool schemas on top. `/tokens middleware` attributes every token to
  the middleware or tool that added it, and a CI baseline fails the build when the
  number creeps up.

### 6. Runs literally anywhere

The same agent, driven six different ways.

```bash
bog-agents                                  # interactive matte-swamp TUI
bog-agents -p "explain this module" < src/agent.py     # one-shot, clean stdout
bog-agents -n "fix the failing test" --auto --json     # headless, structured
bog-agents --plan "add rate limiting" --auto           # plan, review, then execute
bog-agents --drive smoke.yaml               # scripted TUI run (Pilot-backed, JSONL out)
bog-agents mcp-server                        # BE an MCP server — let Cursor/Zed delegate to it
bog-agents-daemon run                        # cron / webhook / git-push, unattended
```

`--restricted`, `--mini`, and `--sandbox docker` compose with any of these.

---

## Why Bog Agents

- **Resilient by default.** Failures retry with bounded backoff, hung commands
  time out, and a provider hiccup never kills the run. A crash drops a redacted
  panic dump so the bug report writes itself.
- **Secure where it counts.** Secure-by-default backends, an optional OS-level
  sandbox with a network egress allowlist, a memory-only secrets vault that
  never touches disk, and tokens written atomically at `0o600` before the rename.
- **Governed autonomy.** Agent teams, best-of-N with a rubric judge, and
  multi-reviewer diff votes — all bounded by hard cost caps and packaged with
  proof-of-work evidence, so you can trust a run you didn't watch.
- **No ceremony.** `pipx install bog-agents-cli && bog-agents` puts a working
  agent in front of you in under a minute; `pip install bog-agents` plus one
  function call embeds one in your code.
- **Composable to the core.** ~90 middlewares snap on or off, sub-agents nest,
  backends swap. The framework gets out of the way as your needs sharpen.
- **Windows-first, not Windows-eventually.** A one-line installer, a standalone
  no-Python zip, and a native Windows story for the sandbox and the daemon.

---

## Quick install

### CLI (most folks start here)

```bash
# pipx is recommended — isolated install, clean PATH
pipx install bog-agents-cli

# or with uv (fastest)
uv tool install bog-agents-cli

# or plain pip
pip install bog-agents-cli
```

No Python, no package manager, or on Windows? One line picks the right path
(uv → pipx → pip, installs uv and a Python when the machine has none, warns
about the Microsoft Store `python`/`pwsh` aliases, fixes PATH, runs the doctor):

```powershell
irm https://raw.githubusercontent.com/bogware/bog-agents/main/install.ps1 | iex   # Windows
```

```bash
curl -LsSf https://raw.githubusercontent.com/bogware/bog-agents/main/install.sh | sh  # macOS / Linux
```

Every CLI release also attaches a standalone `bog-agents-<version>-windows-x64.zip`
(no Python required; unzip and run `bog-agents\bog-agents.exe`) — see
[`packaging/`](packaging/README.md) for the winget manifest and Homebrew formula.

Provider extras:

```bash
pip install 'bog-agents-cli[anthropic]'        # Claude
pip install 'bog-agents-cli[openai]'           # GPT
pip install 'bog-agents-cli[bedrock]'          # AWS Bedrock
pip install 'bog-agents-cli[all-providers]'    # everything
```

Set one provider key and ride:

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # or OPENAI_API_KEY, GOOGLE_API_KEY, AWS creds, ...
bog-agents --doctor-deep                # one-page health check
bog-agents                              # interactive TUI
bog-agents -p "explain this module" < src/agent.py   # one-shot
```

No key handy? Point it at a local [Ollama](https://ollama.com/) model and
nothing leaves the machine.

Keep current with `/update` inside the TUI — it checks PyPI, shows what will
download, asks before installing, and upgrades the way you installed (uv tool /
pipx / pip).

### Daemon (ambient runner)

```bash
pip install bog-agents-daemon
bog-agents-daemon run --port 7878
```

See the [daemon README](libs/daemon/README.md) for systemd / Windows-task /
launchd installation, the REST API, trigger types, scan jobs, and output targets.

### SDK only (for embedding agents in your own app)

```bash
pip install bog-agents
```

```python
from bog_agents import create_agent

agent = create_agent(model="anthropic:claude-sonnet-4-6")
result = await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]})
```

See the [SDK README](libs/bog-agents/README.md) for backends, middlewares,
governed-autonomy primitives, deepagents compatibility, and the full provider
matrix.

---

## The everyday commands

The 140+ slash commands span far more than this, but these are the ones you'll
reach for daily. The full grouped reference lives in
[`docs/cli/commands.md`](docs/cli/commands.md).

| | |
|---|---|
| `/model`, `/effort`, `/operator` | Switch models, set the reasoning knob, auto-route by difficulty |
| `/plan`, `/review-plan` | Plan-then-act; review a plan line by line before it runs |
| `/review`, `/self-review`, `/jury` | Structured review, the five-lens pre-submit gate, a jury vote on a diff |
| `/team`, `/best-of-n` | A governed team over a task ledger; N judged worktree attempts |
| `/workflow`, `/<name>` | Author and run agent-authored multi-phase workflows |
| `/findings`, `/remediate` | The findings ledger; turn a finding into a fix |
| `/cost`, `/usage`, `/changes` | Where the money went; per-response usage; the turn-end changes tray |
| `/permissions`, `/actionlog` | Trust posture and org policy; the hash-chained audit trail |
| `/memory`, `/threads`, `/recap` | Consolidate memory; browse threads; where this session stands |
| `/mcp`, `/skills`, `/plugin` | MCP marketplace + manager; skills; agent plugins |
| `/tasks`, `/subtask`, `/fork` | The task command center; background sub-tasks; fork the conversation |

Type `/` and start typing — fuzzy autocomplete surfaces what you need.

---

## Repository layout

| Path | What |
|---|---|
| `libs/bog-agents/` | The Python SDK. Compiled LangGraph agents, ~90 middlewares, pluggable backends, tool bundles, governed-autonomy primitives, deepagents compatibility. |
| `libs/cli/` | The terminal CLI. Textual TUI, 140+ slash commands, MCP marketplace, headless command surface, `bog-agents --drive` scripted runner. |
| `libs/daemon/` | The ambient daemon. Cron / file-watch / webhook / git-push triggers, scan jobs, REST API. |
| `libs/acp/` | Agent Client Protocol bridge for the Zed editor. |
| `libs/harbor/` | Evaluation / benchmark harness (Terminal Bench 2.0). |
| `libs/partners/` | Sandbox provider integrations (first-party source today: Daytona). |
| `libs/vscode-extension/` | VS Code integration. |

Each package has its own `pyproject.toml`, `Makefile`, and version. SDK / CLI /
daemon are released together on synchronized versions; the rest tag
independently.

---

## Working from source

```bash
git clone https://github.com/bogware/bog-agents
cd bog-agents

# Install + run the CLI from source
cd libs/cli
uv sync --reinstall
uv run bog-agents
```

Every package answers the same Makefile targets:

```bash
make test        # unit tests, no network
make lint        # ruff check + ruff format --diff + ty
make format      # ruff fix + ruff format
```

CI runs `make lint` + `make test` for the SDK, CLI, and daemon on Linux and
Windows across Python 3.11–3.13, `make test` for the satellites (acp, harbor,
daytona), and a lockfile-drift check across all packages. See
`.github/workflows/ci.yml`.

---

## Documentation

Start with **[`docs/`](docs/)** — the full documentation tree:

- **[Getting Started](docs/getting-started.md)** — install, first run, the five
  commands you'll use forever
- **[Command reference](docs/cli/commands.md)** — every slash command, grouped
- **[Governed autonomy](docs/cli/governed-autonomy.md)** — teams, best-of-N,
  jury, workflows, evidence, cost caps
- **[Findings & security scans](docs/cli/findings.md)** — the ledger, the
  security-scan recipe, the CI gate, `/remediate`
- **[Governance & safety](docs/cli/governance.md)** — trust profiles, managed
  policy, the OS sandbox, the hook bus, the action log
- **[Cookbook](docs/cookbook.md)** — task-shaped recipes
- **[Troubleshooting](docs/troubleshooting.md)** · **[Tips & Tricks](docs/tips-and-tricks.md)**
- **[Security Model](docs/security.md)** — what's safe by default, threat boundaries
- **CLI deep dives**: [Drive runner](docs/cli/drive.md)
- **SDK guides**: [Quickstart](docs/sdk/quickstart.md) ·
  [Middleware](docs/sdk/middleware.md) · [Tool bundles](docs/sdk/tool-bundles.md)
- **Providers**: [AWS Bedrock](docs/providers/bedrock.md)
- **Daemon**: [Quickstart](docs/daemon/quickstart.md)
- **Advanced**: [Expert Rules](docs/advanced/expert-rules.md)

Also:

- **Per-package READMEs**: [SDK](libs/bog-agents/README.md) ·
  [CLI](libs/cli/README.md) · [Daemon](libs/daemon/README.md)
- **Architecture** — [`CLAUDE.md`](CLAUDE.md)
- **Roadmap** — [`ROADMAP.md`](ROADMAP.md) · **Audit** — [`REVIEW.md`](REVIEW.md)
- **Changelog** — [`CHANGELOG.md`](CHANGELOG.md)
- **Issues** — <https://github.com/bogware/bog-agents/issues>

---

## License

MIT. See [LICENSE](LICENSE).

---

*Pass through in harmony.*
