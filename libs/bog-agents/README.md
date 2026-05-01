# Bog Agents

The Python SDK underneath [`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/) and [`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/). One `create_agent()` call gets you a compiled LangGraph agent with file tools, a shell, git, sub-agents, plan mode, auto-quality checks, and 80-some composable middleware. Pluggable backends. Any tool-calling LLM.

[![PyPI](https://img.shields.io/pypi/v/bog-agents)](https://pypi.org/project/bog-agents/)
[![License](https://img.shields.io/pypi/l/bog-agents)](https://opensource.org/licenses/MIT)
[![Downloads](https://img.shields.io/pepy/dt/bog-agents)](https://pypistats.org/packages/bog-agents)

---

## Install

```bash
pip install bog-agents
# or
uv add bog-agents
```

Python 3.11 or better. Bring your own provider package — Anthropic, OpenAI, AWS, Google,
local Ollama, doesn't matter. Pick one or pick all of them.

---

## First run

```python
from bog_agents import create_agent

agent = create_agent()  # default: anthropic:claude-sonnet-4-6

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Hello!"}]},
    config={"configurable": {"thread_id": "my-thread"}},
)
```

Pick whatever model you've got the keys for:

```python
agent = create_agent(model="openai:gpt-5.4")
agent = create_agent(model="bedrock_converse:us.anthropic.claude-sonnet-4-6")
agent = create_agent(model="google_genai:gemini-2.5-pro")
agent = create_agent(model="ollama:gpt-oss:20b")  # local, free
```

---

## What it does

**Builds the agent.** `create_agent()` returns a compiled LangGraph graph wired with whatever middleware stack you ask for. Default is sensible. Power users override every piece.

**Talks to anything.** Anthropic, OpenAI, AWS Bedrock, Google AI / Vertex AI, DeepSeek, Mistral, Groq, NVIDIA, Ollama, Cohere, xAI, Perplexity, Fireworks, OpenRouter, Together, HuggingFace — anything LangChain knows about, this handles.

**Reads, writes, edits, runs, commits.** Filesystem tools, shell tools, git tools — all pluggable. Local backend by default; swap in a remote sandbox (Modal, Daytona, Runloop, LangSmith) when you want isolation.

**Sub-agents.** Spawn child agents to parallelize work that doesn't need to share state.

**Plan mode and checkpoints.** Read-only plan mode for scouting; git-based snapshots before mutations so you can roll back without thinking about it.

**Production hooks.** `bog_agents.serve.AgentServer` exposes a REST + SSE API. `bog-agents-daemon` schedules ambient runs on cron, file-change, webhook, and git-push triggers. Built for the long haul, not just a demo.

---

## A loaded example

When you want the whole posse:

```python
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    enable_git_tools=True,
    enable_repo_map=True,
    enable_checkpointing=True,
    enable_cost_tracking=True,
    enable_plan_mode=True,
    auto_lint=True,
    working_dir="/path/to/project",
)

result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Fix the failing tests"}]},
    config={"configurable": {"thread_id": "my-session"}},
)
```

---

## Middleware

Eighty-some pieces in the stack. Mix what you need; leave the rest in the wagon.

| Middleware | What it does |
|-----------|-------------|
| `FilesystemMiddleware` | read / write / edit / multi-edit / glob / grep |
| `GitToolsMiddleware` | status, diff, log, commit, add, branch, stash, blame, show |
| `RepoMapMiddleware` | symbol-extracted code map (Python, JS, TS, Rust, Go, Java) |
| `CheckpointingMiddleware` | git-based snapshot before mutations + diff/undo |
| `CostTrackerMiddleware` | tokens, cost, budgets, effort levels |
| `PlanModeMiddleware` | read-only mode that blocks mutating tools |
| `AutoQualityMiddleware` | auto-lint / auto-test after edits with project detection |
| `ArchitectMiddleware` | dual-model architect ↔ reviewer cross-talk |
| `ParallelAgentsMiddleware` | concurrent sub-agent execution |
| `LifecycleHooksMiddleware` | 15 event types for external tool integration |
| `ContextPackingMiddleware` | structured context compression |
| `SummarizationMiddleware` | auto-summarize when the context window fills |
| `MemoryMiddleware` | persistent `AGENTS.md` memory across sessions |
| `SkillsMiddleware` | custom skill / instruction loading |
| `SafeToolsConfig` | per-tool auto-approval rules |

---

## Run as an HTTP server

For when something else needs to drive.

```bash
pip install 'bog-agents[serve]'
```

```python
from bog_agents import create_agent
from bog_agents.serve import AgentServer

agent = create_agent()
server = AgentServer(agent)
server.run()  # http://127.0.0.1:8420
```

Endpoints out of the box:

```
GET  /health
GET  /info
GET  /openapi.json     <-- hand-rolled OpenAPI 3.0 schema; Swagger UI works
POST /invoke
POST /stream            <-- Server-Sent Events
POST /threads
GET  /threads
POST /threads/{id}/messages
GET  /threads/{id}/history
```

---

## Companion packages

| Package | What it's for |
|---------|---------------|
| **[`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/)** | TUI and non-interactive CLI. Drives an agent from your terminal. |
| **[`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/)** | Ambient agent scheduler. Cron, file-watch, webhooks, git push. |

All three release together with the same version number.

---

## Security model

`LocalShellBackend.execute()` runs commands on the host with `shell=True`. **No sandbox. No process isolation.** That's by design — when you're in your own checkout you don't want a wall in the way. When you're running someone else's input, set up a remote sandbox backend (`ModalBackend`, `DaytonaBackend`, `RunloopBackend`, `LangSmithBackend`) and route the same agent through it.

The shell decodes child output as UTF-8 with `errors='replace'`, so output containing checkmarks, ANSI escapes, or box-drawing characters never trips the Windows cp1252 reader. Recurring fix across 0.7.3.

For Bedrock specifically, the SDK's credential probe falls back from an expired SSO session to static `~/.aws/credentials` keys automatically — see `[models.providers.bedrock] auth_mode` in the CLI's `config.toml`, or set `BOG_AGENTS_BEDROCK_AUTH_MODE`. New in 0.7.4.

---

## Resources

- [Repository + issue tracker](https://github.com/bogware/bog-agents)
- [Releases](https://github.com/bogware/bog-agents/releases) — synced notes for SDK, CLI, and daemon

---

## Contributing

[Contributing Guide](https://github.com/bogware/bog-agents/blob/main/CONTRIBUTING.md). Conventional Commits required (`feat:`, `fix:`, etc.). 150-char line length. ruff + ty clean. Full test suite green per package, no fork-of-fork lineage drift.

---

## License

MIT.

---

*Saddle up.*
