# Bog Agents

> *Patient as still water. Opinionated where it matters. Pass through in harmony.*

**v0.8.7** — a production-ready AI agent framework built on LangGraph,
deliberately calm by design.

Three packages, one philosophy:

- **[`bog-agents`](libs/bog-agents)** — the Python SDK. `create_agent()` returns a
  compiled LangGraph agent with file tools, a shell, sub-agents, plan mode,
  retry-with-backoff, and ~80 composable middlewares.
- **[`bog-agents-cli`](libs/cli)** — a coding agent that lives in your terminal.
  120+ slash commands, any LLM, persistent memory, MCP marketplace, `/peat`
  personal scheduler, `/qa` acceptance-criteria harness, `/record` + `/replay`,
  in-memory secrets vault, `bog-agents drive` for non-interactive scripted
  runs, matte-swamp / neon-green TUI.
- **[`bog-agents-daemon`](libs/daemon)** — the patient watcher. Runs your
  agents on cron / file-change / webhook / git-push triggers; survives
  reboots; reports back via Slack / email / GitHub / file / webhook.

Built on [LangGraph](https://github.com/langchain-ai/langgraph). MIT.

---

## Philosophy

Most agent frameworks make you assemble the kit. We don't. Bog Agents starts
you with a working agent — and lets you peel away or bolt on layers as you
understand what you actually need.

- **Patient by default.** Failures retry with bounded backoff. Hung commands
  time out. Provider hiccups don't kill the run. Crashes drop a redacted
  panic dump for easy bug reports.
- **Opinionated where it matters.** Secure-by-default backends. A
  memory-only secrets vault that never touches disk. Structured event
  logging at every chokepoint. Tokens written atomically with `0o600`
  before the rename — no world-readable race window.
- **No ceremony.** `pipx install bog-agents-cli && bog-agents` and you have
  a working agent in under a minute. `pip install bog-agents` and one
  function call gets you a compiled agent.
- **Composable.** 80+ middlewares snap on or off. Subagents nest.
  Backends swap. The framework gets out of your way.

The bog is calm, deep, and unhurried. So is the agent.

---

## Quick install

### CLI (most users start here)

```bash
# pipx is recommended — isolated install, clean PATH
pipx install bog-agents-cli

# or with uv (fastest)
uv tool install bog-agents-cli

# or plain pip
pip install bog-agents-cli
```

Provider extras:

```bash
pip install 'bog-agents-cli[anthropic]'        # Claude
pip install 'bog-agents-cli[openai]'           # GPT
pip install 'bog-agents-cli[all-providers]'    # everything
```

Then run:

```bash
bog-agents --doctor-deep      # one-page health summary
bog-agents                    # interactive TUI
bog-agents -p "explain this module" < src/agent.py   # one-shot
```

### Daemon (ambient runner)

```bash
pip install bog-agents-daemon
bog-agents-daemon run --port 7878
```

See the [daemon README](libs/daemon/README.md) for systemd / Windows-task /
launchd installation, REST API, trigger types, and output targets.

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
and the full provider matrix.

---

## What's new since 0.8.0

Successive waves have hardened the framework toward a credible 1.0:

- **Wave Y (0.8.8)** — Release-readiness hardening. Audit-trail strict
  hook warnings, first-run no-API-key actionable error, preview-server
  cap + cleanup tool, OAuth structured observability, opt-in
  `MessageStore` JSONL persistence for crash recovery, `__all__` on
  headline middleware modules, refreshed docs.
- **Wave X (0.8.7)** — `merge_worktree` ref-injection fix,
  `start_preview_server` `shlex` parsing + DEVNULL, daemon
  `dispatch_errors` capture.
- **Wave W (0.8.6)** — `bog-agents drive` scripted TUI runner with
  YAML grammar + Pilot harness; tool-bundle pattern; canonical
  middleware-ordering test; comprehensive resilience pass.
- **Wave V (0.8.5)** — Stub middleware cleanup, ~7,900 lines net
  deletion; `/causal` → `/trace-mind` rename.
- **Wave U–T (0.8.4–0.8.3)** — architect-audit surgical fixes,
  postmortem → dreamscape proposer feedback loop.
- **Wave S–Q (0.8.2–0.8.0)** — TraceFile v1 (Ed25519 signed open
  trace format), `/compliance` auditor, provable policies +
  postmortem + time-travel replay.

See [CHANGELOG.md](CHANGELOG.md) for the full history. The 0.8.0
notes below cover the original flagship release.

## What's new in 0.8.0

A genuine flagship release. Five top-line capabilities and a hundred small
refinements.

### `/peat` — your personal assistant

A long-lived in-process sub-agent with a hand-crafted persona. Schedules
recurring jobs (cron, `@every`, `@once`), runs deep research with a
five-phase plan, builds personalized digests from your `/qa` results and
`/replay` recordings.

```text
/peat schedule "0 9 * * 1-5 | summarize yesterday's QA results"
/peat research "vector databases" --focus pricing,perf
/peat digest --days 7
/peat metrics
```

### `/qa` — adaptive QA harness

Acceptance-criteria-driven QA plans. Ingest from Jira (via your MCP Jira
tool), file, JSON, or stdin. Hybrid step model — agent / shell / http /
mcp — with verdicts (`exit_code`, `status`, `contains`, `regex`,
`json_path`). Outputs as Markdown, JSON, stdout, or Jira comment.

### `/record` + `/replay` — sessions you can edit and re-run

`/record` captures user prompts, AI responses, and tool calls live.
`/record stop` finalizes to a YAML file with auto-detected variables
(Jira IDs, repo URLs, file paths) replaced by `${var}` placeholders.
`/replay run` prompts for any unfilled variables and dispatches to the
agent.

### Vault + Vars

Typed variable system shared by `/replay` and `/qa`: `string`, `secret`,
`enum`, `int`, `bool`. Secrets live only in process memory; nothing
persists to disk. Optional read-only OS-keychain bridge.

### MCP marketplace, expanded

35+ curated servers across 9 categories: github, jira, gitlab, slack,
postgres, mongodb, redis, bigquery, snowflake, supabase, aws, azure-devops,
terraform, cloudflare, stripe, hubspot, notion, confluence, google-drive,
discord, kubernetes, datadog, sentry, and more.

```text
/mcp marketplace          # browse the catalog
/mcp install jira         # install from the catalog
/mcp add my-tool ...      # custom server
```

### Plus, under the hood

- **`ProviderRetryMiddleware`** — bounded exponential backoff with jitter
  on transient provider errors. Never retries tool calls.
- **`virtual_mode=True` is now the default** for `FilesystemBackend` and
  `LocalShellBackend`. Path traversal blocked unless explicitly opted out
  via `BOG_AGENTS_FS_UNSANDBOXED=1`.
- **Subprocess `stdin=/dev/null`** — interactive commands like Windows
  `date` get an immediate EOF instead of hanging the agent forever.
- **Panic dumps** — uncaught exceptions land at
  `~/.bog-agents/crash/<ts>.log` with redacted host info, versions,
  traceback, and recent metrics. Attach the file when you open an issue.
- **Structured event logging** at every chokepoint, ready for shippers
  (Splunk, Loki, journald). Stable event names, prefixed `evt_*` fields.
- **Mouse-tracking escape-sequence swallower** so moving the mouse over
  the terminal during a long agent run no longer leaks `[<35;16;41M`
  garbage into your input box.
- **`--doctor-deep`** — runtime probes of every external dependency
  (Python, dirs writable, settings parseable, git, provider envs, network
  reachability, MCP config, recent crashes) in under a second.

See [CHANGELOG.md](CHANGELOG.md) for the full 0.8.0 entry.

---

## Repository layout

| Path | What |
|---|---|
| `libs/bog-agents/` | The Python SDK. Compiled LangGraph agents, ~80 middlewares, pluggable backends, tool bundles. |
| `libs/cli/` | The terminal CLI. Textual TUI, 120+ slash commands, MCP marketplace, `bog-agents drive` scripted runner. |
| `libs/daemon/` | The ambient daemon. Cron / file-watch / webhook triggers, REST API. |
| `libs/acp/` | Agent Client Protocol bridge for the Zed editor. |
| `libs/harbor/` | Evaluation / benchmark harness (Terminal Bench 2.0). |
| `libs/partners/` | Sandbox provider integrations (today: Daytona). |
| `libs/vscode-extension/` | VS Code integration. |

Each package has its own `pyproject.toml`, `Makefile`, and version. SDK / CLI /
daemon are released together; the rest tag independently.

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

Each package has the same Makefile targets:

```bash
make test        # unit tests, no network
make lint        # ruff check + ruff format --diff + ty
make format      # ruff fix + ruff format
```

CI runs `make lint` + `make test` per package on every PR. See
`.github/workflows/ci.yml`.

---

## Documentation

Start with **[`docs/`](docs/)** — the full documentation tree:

- **[Getting Started](docs/getting-started.md)** — install, first
  run, the five commands you'll use forever
- **[Cookbook](docs/cookbook.md)** — fifteen task-shaped recipes
- **[Troubleshooting](docs/troubleshooting.md)** — every common
  error, what it means, how to fix it
- **[Tips & Tricks](docs/tips-and-tricks.md)** — power-user moves
- **[Security Model](docs/security.md)** — what's safe by default,
  what isn't, threat boundaries
- **CLI deep dives**: [Drive runner](docs/cli/drive.md) ·
  [Slash commands](docs/cli/slash-commands.md)
- **SDK guides**: [Quickstart](docs/sdk/quickstart.md) ·
  [Middleware](docs/sdk/middleware.md) ·
  [Tool bundles](docs/sdk/tool-bundles.md)
- **Daemon**: [Quickstart](docs/daemon/quickstart.md)
- **Advanced**: [Expert Rules](docs/advanced/expert-rules.md)

Also:

- **Per-package READMEs**: [SDK](libs/bog-agents/README.md) ·
  [CLI](libs/cli/README.md) · [Daemon](libs/daemon/README.md)
- **Architecture** — [`CLAUDE.md`](CLAUDE.md)
- **Changelog** — [`CHANGELOG.md`](CHANGELOG.md)
- **Issues** — <https://github.com/bogware/bog-agents/issues>

---

## License

MIT. See [LICENSE](LICENSE).

---

*Pass through in harmony.*
