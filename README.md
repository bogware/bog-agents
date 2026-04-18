# Bog Agents

**v0.7.0** — Production-ready AI agent framework built on LangGraph.

Bog Agents gives you two things:

- `bog-agents`: a Python SDK for building agentic workflows on LangGraph
- `bog-agents-cli`: a terminal-first coding agent for day-to-day engineering work

Out of the box: file tools, shell execution, thread history, model switching, vault-backed API key management, parallel worktree agents, MCP integration, background work, codebase indexing, interactive PR review, and a practical human-in-the-loop approval model.

Built on [LangGraph](https://github.com/langchain-ai/langgraph). MIT licensed.

---

## Why Bog Agents

- **Production-minded SDK** — start with a compiled LangGraph graph; extend with composable middleware instead of rebuilding primitives for every project
- **Parallel agent architecture** — spawn multiple sub-agents in isolated git worktrees, detect merge conflicts pre-flight, synthesize results automatically
- **Serious CLI** — interactive Textual TUI, non-interactive automation, `/checkpoint` resume, `/explain` deep-dives, `/pr review` for GitHub and Azure DevOps
- **Vault-first secrets** — API keys stored in an encrypted vault (`/vars set`), injected into the environment at startup so every provider, MCP server, and LangSmith tracing connection just works
- **Model flexibility** — any LangChain-compatible tool-calling model: Anthropic, OpenAI, Ollama, Bedrock, Google, Mistral, Groq, xAI, and more

---

## Quick Install

### CLI (recommended)

```bash
# Anthropic (Claude)
pip install 'bog-agents-cli[anthropic]'

# OpenAI
pip install 'bog-agents-cli[openai]'

# All providers
pip install 'bog-agents-cli[all-providers]'
```

With `uv` (faster):

```bash
uv tool install 'bog-agents-cli[anthropic]'
```

### SDK only

```bash
pip install bog-agents
```

---

## Run Locally

### Standard (Anthropic / OpenAI)

```bash
# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."   # or OPENAI_API_KEY

# Start the interactive TUI
bog-agents

# Or store the key in the vault so you never set it again
bog-agents
# then inside: /vars set ANTHROPIC_API_KEY sk-ant-...
```

### With Ollama (local models, no API key needed)

```bash
# 1. Install Ollama — https://ollama.ai
brew install ollama          # macOS
# or: curl -fsSL https://ollama.ai/install.sh | sh

# 2. Pull a model
ollama pull llama3.2          # 3B, fast
ollama pull qwen2.5-coder     # excellent for coding tasks
ollama pull mistral-nemo      # good balance

# 3. Run Bog Agents with that model
pip install 'bog-agents-cli[ollama]'
bog-agents -M ollama:llama3.2

# Or set it as the permanent default
bog-agents
# then inside: /model set ollama:llama3.2
```

Ollama runs entirely locally — no API keys, no usage costs, no data leaves your machine.

---

## Run From Source

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/bogware/bog-agents.git
cd bog-agents

# Install CLI dependencies
cd libs/cli
uv sync

# Run the CLI
uv run bog-agents

# With a specific model
uv run bog-agents -M anthropic:claude-sonnet-4-6
uv run bog-agents -M ollama:llama3.2

# Verify environment
uv run bog-agents --doctor
```

---

## SDK Quick Start

```python
from bog_agents import create_agent

# Basic agent
agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    enable_git_tools=True,
    enable_cost_tracking=True,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Summarize the testing strategy."}]},
    config={"configurable": {"thread_id": "demo-thread"}},
)

# Parallel worktree agent (spawn sub-agents in isolated git branches)
agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    enable_parallel_worktree=True,   # ParallelWorktreeMiddleware with default factory
    enable_result_synthesis=True,    # auto-synthesize results when tasks complete
)
# Agent can now use spawn_parallel_tasks, await_tasks_complete, synthesize_parallel_results tools
```

`create_agent()` returns a compiled LangGraph graph — streaming, checkpointers, Studio, and remote execution all work without wrapping.

---

## CLI Reference

### Interactive session

```bash
bog-agents                          # start TUI
bog-agents -M claude-sonnet-4-6    # pick model
bog-agents -M ollama:llama3.2      # local model
bog-agents -r                       # resume last thread
bog-agents -r <thread-id>           # resume specific thread
```

### Non-interactive / automation

```bash
bog-agents -n "Summarize repo status"
bog-agents -p "Explain this module" < path/to/file.py
bog-agents -n "List TODOs" --json
bog-agents -n "Run tests and explain failures" --shell-allow-list recommended
bog-agents -n "Fix failing tests and commit" --shell-allow-list all
```

### Slash commands (inside TUI)

| Command | What it does |
|---------|--------------|
| `/help` | Search commands by keyword |
| `/model` | Switch model or set default |
| `/checkpoint save <name>` | Save a named session checkpoint |
| `/checkpoint load <name>` | Restore a checkpoint |
| `/explain <symbol or file>` | Deep-dive explanation with call sites |
| `/index build` | Build TF-IDF codebase knowledge index |
| `/index search <query>` | Search the index |
| `/pr review <number>` | Fetch and review a GitHub/Azure DevOps PR diff |
| `/test run [file]` | Auto-detect framework and run tests |
| `/benchmark run [suite]` | Run evaluation suites |
| `/agent panel` | Live parallel agent status dashboard |
| `/agent spawn --worktree <prompt>` | Spawn a sub-agent in an isolated git worktree |
| `/team sync` | Git-based shared memory sync (pull/push/both) |
| `/undo restore <path>` | Git-backed safe file restore |
| `/vars set KEY value` | Store API key in encrypted vault |
| `/langsmith set-key <key>` | Enable LangSmith tracing |
| `/mcp` | Browse and manage MCP servers |
| `/skills` | Show loaded skills and search paths |
| `/plan` | Toggle read-only plan mode |
| `/review` | Structured code review |
| `/resume` | Resume a previous thread |
| `/compact` | Summarize context to reduce token usage |
| `/doctor` | Environment diagnostics |

Type `/` in the TUI to search all commands with fuzzy matching.

---

## Parallel Agent Architecture

Bog Agents has first-class support for spawning multiple sub-agents that work in parallel and merging their results.

```python
# Enable parallel worktrees + result synthesis in one line
agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    enable_parallel_worktree=True,
    enable_result_synthesis=True,
)
```

The agent gains these tools automatically:

| Tool | What it does |
|------|-------------|
| `spawn_parallel_tasks` | Launch N tasks in isolated git worktrees concurrently |
| `worktree_status` | Check status of all running tasks |
| `merge_task_results` | Merge completed branches with conflict detection |
| `await_tasks_complete` | Wait for tasks to finish (async, with timeout) |
| `synthesize_parallel_results` | Build a structured synthesis prompt for the agent |
| `gather_parallel_results` | Collect + format all task outputs |

**Smart merge strategies** — when merging parallel branches back, choose from:
- `prefer_source` — source branch wins on conflicts (`-X theirs`)
- `prefer_target` — main branch wins (`-X ours`)
- `sequential` — detect conflicts and retry tasks one-by-one
- `manual` — surface conflicts for human review (default)

Trivial whitespace-only conflicts are auto-resolved regardless of strategy.

---

## Vault / API Key Management

Store API keys once; every session uses them automatically.

```bash
# In the TUI
/vars set ANTHROPIC_API_KEY sk-ant-...
/vars set OPENAI_API_KEY sk-...
/vars set LANGSMITH_API_KEY lsv2_...

# Or use provider-specific helpers
/langsmith set-key lsv2_...
```

Keys stored in the vault are injected into `os.environ` at startup. Downstream libraries (LangChain, LangSmith, Daytona, etc.) pick them up transparently. Environment variables set in the shell always take precedence over vault values.

Supported keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `COHERE_API_KEY`, `NVIDIA_API_KEY`, `FIREWORKS_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `LANGSMITH_API_KEY`, `DAYTONA_API_KEY`.

---

## Monorepo Structure

```text
libs/
├── bog-agents/      # Core SDK — create_agent(), middleware, backends
├── cli/             # Terminal UI — Textual TUI, slash commands, vault
├── acp/             # Agent Context Protocol (Zed editor)
├── harbor/          # Evaluation / benchmark framework
├── vscode-extension/# VS Code extension
└── partners/        # Sandbox integrations (Daytona, Modal, Runloop, QuickJS)
```

---

## Models and Providers

| Provider | Example model string |
|----------|---------------------|
| Anthropic | `anthropic:claude-sonnet-4-6` |
| Anthropic Opus | `anthropic:claude-opus-4-7` |
| OpenAI | `openai:gpt-4o` |
| Ollama (local) | `ollama:llama3.2`, `ollama:qwen2.5-coder` |
| Google | `google_genai:gemini-2.5-pro` |
| Groq | `groq:llama-3.3-70b-versatile` |
| Mistral | `mistral:mistral-large-latest` |
| Bedrock | `bedrock_converse:anthropic.claude-sonnet-4-6` |
| xAI | `xai:grok-2` |
| OpenRouter | `openrouter:meta-llama/llama-3` |

Use `--doctor` or `/doctor` when provider setup does not behave as expected.

---

## Remote Execution and MCP

```bash
# Sandbox-backed sessions
bog-agents --sandbox modal
bog-agents --sandbox daytona
bog-agents --sandbox runloop

# MCP configuration
bog-agents --mcp-config ./mcp.json
bog-agents --trust-project-mcp

# HTTP server mode
bog-agents --serve
bog-agents --serve --serve-host 0.0.0.0 --serve-port 9000

# ACP server (Zed editor integration)
bog-agents --acp
```

---

## Development

```bash
# Lint all packages
make lint

# Format all packages
make format

# Package-level tests
cd libs/bog-agents && uv run --group test pytest tests/unit_tests/ -q
cd libs/cli       && uv run --group test pytest tests/unit_tests/ -q
```

Contributor guidance: `AGENTS.md` (agent-specific), `CONTRIBUTING.md` (human contributors).

---

## Links

- [CLI package README](libs/cli/README.md)
- [Contributing](CONTRIBUTING.md)
- [Publishing](PUBLISHING.md)
- [LangGraph docs](https://github.com/langchain-ai/langgraph)
