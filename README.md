# Bog Agents

Bog Agents is an open-source agent framework for software work.

It gives you two things:

- `bog-agents`: a Python SDK for building agentic workflows on top of LangGraph
- `bog-agents-cli`: a terminal-first coding agent for day-to-day engineering work

The project is designed for teams that want real agents, not a pile of prompt glue. Out of the box you get file tools, shell execution, thread history, model switching, memory, skills, MCP integration, background work, and a practical human-in-the-loop approval model.

Built on [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview). MIT licensed.

## Why Bog Agents

- Production-minded SDK: start with a compiled LangGraph graph and extend it with middleware instead of rebuilding the basics for every project.
- Serious CLI: use one tool for interactive coding, non-interactive automation, diagnostics, thread resume, and review workflows.
- Model flexibility: works with LangChain-compatible tool-calling models, including Anthropic, OpenAI, Ollama, Bedrock, Google, and more.
- Monorepo architecture: SDK, CLI, protocol support, evaluation tooling, and partner integrations live together and evolve together.

## Install

### SDK

```bash
pip install bog-agents
```

### CLI

```bash
pip install bog-agents-cli

# Provider extras
pip install 'bog-agents-cli[anthropic]'
pip install 'bog-agents-cli[ollama]'
pip install 'bog-agents-cli[all-providers]'
```

With `uv`:

```bash
uv tool install 'bog-agents-cli[anthropic]'
```

The CLI includes OpenAI support by default. Other providers are enabled through extras.

## SDK Quick Start

```python
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    enable_git_tools=True,
    enable_cost_tracking=True,
)

result = agent.invoke(
    {
        "messages": [
            {
                "role": "user",
                "content": "Inspect this repository and summarize the testing strategy.",
            }
        ]
    },
    config={"configurable": {"thread_id": "demo-thread"}},
)
```

`create_agent()` returns a compiled LangGraph graph. That means you can use streaming, checkpointers, Studio, remote execution, and the rest of the LangGraph ecosystem without wrapping Bog Agents in another orchestration layer.

## CLI Quick Start

Start an interactive session:

```bash
bog-agents
```

Pick a model explicitly:

```bash
bog-agents -M claude-sonnet-4-6
bog-agents -M gpt-4o
bog-agents -M ollama:llama3
```

Verify your environment:

```bash
bog-agents --doctor
```

Resume a previous session:

```bash
bog-agents -r
bog-agents -r <thread-id>
```

## CLI In Depth

### Interactive workflow

`bog-agents-cli` is built around a Textual terminal UI. It is optimized for long-running coding sessions where the agent needs to inspect files, use tools, ask for approval when appropriate, and keep enough state to be useful across turns.

Inside the app, type `/` to browse commands or use `/commands` and `/help <keyword>` to search the command surface.

Representative interactive commands:

| Command | Purpose |
| --- | --- |
| `/commands` | Browse available slash commands with descriptions |
| `/model` | Switch models or manage the default model |
| `/resume` | Resume the most recent thread or a specific thread |
| `/session` | Show session details or assign a local label |
| `/permissions` | Inspect approval and shell policy |
| `/keybindings` | Show active keybindings or the config path |
| `/skills` | Show loaded skills and search paths |
| `/review` | Send a structured review request to the agent |
| `/recommend` | Run configurable recommendation and review flows |
| `/background` | Submit and monitor background agent tasks |
| `/dashboard` | Show a multi-agent status snapshot |
| `/doctor` | Run local environment diagnostics |
| `/logs` | Show the log path and recent warnings or errors |
| `/trace` | Open the current thread in LangSmith |
| `/mcp` | Inspect active MCP servers and tools |
| `/compact` | Reduce context pressure by summarizing history |
| `/clear` | Start a fresh thread |
| `/init` | Generate an `AGENTS.md` for the current repository |
| `/onboard` | Start an interactive codebase tour |

### Threading, memory, and continuity

The CLI is designed for iterative work rather than one-shot prompts.

- Conversations are stored as threads and can be resumed later.
- `/resume`, `/threads`, and `-r` make it practical to jump back into unfinished work.
- `/remember` can capture project knowledge into durable memory and skills.
- Session metadata such as the active thread, model, and current context are visible from the interface instead of being hidden state.

### Safety model

Bog Agents treats tool execution as a policy problem, not a prompt problem.

- Tool and shell approvals are explicit when the current policy requires them.
- `Shift+Tab` toggles auto-approve for the active session.
- `/permissions` shows the current posture so users can see what is allowed before they run a task.
- Non-interactive shell access is opt-in via `--shell-allow-list`.

### Non-interactive mode

The CLI also works well in scripts, CI jobs, and local automation.

```bash
# One-shot task
bog-agents -n "Summarize the current repository status"

# Pipe-friendly output
bog-agents -p "Explain this module" < path/to/file.py

# Machine-readable output
bog-agents -n "List TODO comments" --json

# Allow a curated shell command set
bog-agents -n "Run the tests and explain failures" --shell-allow-list recommended

# Allow specific commands only
bog-agents -n "Search logs for errors" --shell-allow-list cat,grep,find

# Full shell access in a trusted environment
bog-agents -n "Fix the failing tests and commit the result" --shell-allow-list all
```

Exit codes are intended to be automation-friendly:

- `0`: success
- `1`: failure
- `130`: interrupted

### Models and providers

Use the `provider:model` form when you want to be explicit.

| Provider | Example |
| --- | --- |
| Anthropic | `anthropic:claude-sonnet-4-6` |
| OpenAI | `openai:gpt-4o` |
| Ollama | `ollama:llama3` |
| Google | `google_genai:gemini-2.5-pro` |
| Bedrock | `bedrock_converse:anthropic.claude-sonnet-4-6` |
| OpenRouter | `openrouter:meta-llama/llama-3` |
| Perplexity | `perplexity:sonar-pro` |
| xAI | `xai:grok-2` |

The CLI can also infer a provider from your configuration and available credentials. Use `--doctor` or `/doctor` when setup does not behave as expected.

### Remote execution, MCP, and server modes

Bog Agents is not limited to a single local terminal process.

```bash
# Reuse or create a sandbox-backed session
bog-agents --sandbox modal
bog-agents --sandbox daytona
bog-agents --sandbox runloop

# MCP configuration
bog-agents --mcp-config ./mcp.json
bog-agents --no-mcp
bog-agents --trust-project-mcp

# Serve over HTTP
bog-agents --serve
bog-agents --serve --serve-host 0.0.0.0 --serve-port 9000

# Run as an ACP server
bog-agents --acp
```

## Run From Source

Requirements:

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/bogware/bog-agents.git
cd bog-agents

# Bootstrap the main development packages
./scripts/repo.ps1 init

# Run the CLI from source
cd libs/cli
uv run bog-agents
```

The repository is a Python monorepo. The main package areas are:

```text
libs/
|- bog-agents        # core SDK
|- cli               # interactive and automation CLI
|- acp               # Agent Context Protocol support
|- harbor            # evaluation and benchmarking
|- partners/         # sandbox and integration packages
```

## Upgrade Workflow

Use the repository script to validate or refresh lockfiles across the managed packages:

```bash
# Check the primary development packages
./scripts/repo.ps1 lock-check

# Check every managed package under libs/
./scripts/repo.ps1 lock-check -AllPackages

# Regenerate lockfiles
./scripts/repo.ps1 lock -AllPackages
```

## Development

Common top-level tasks:

```bash
make lint
make format
```

Package-level work usually happens inside the package directory with `uv`:

```bash
cd libs/bog-agents
uv run --group test pytest tests/unit_tests/test_specific.py

cd ../cli
uv run --group test pytest tests/unit_tests/test_specific.py
```

The repo guidance for contributors and coding agents lives in `AGENTS.md`, and the broader contribution process lives in `CONTRIBUTING.md`.

## Documentation

- [CLI package README](libs/cli/README.md)
- [Contributing](CONTRIBUTING.md)
- [Publishing](PUBLISHING.md)

## Acknowledgements

Bog Agents began by studying the strengths of general-purpose coding agents and then pushing toward a more open, composable, terminal-native implementation.
