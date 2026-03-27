# Bog Agents CLI

A coding agent in your terminal. Point it at the problem, step back, let it work.

No scaffolding, no boilerplate, no configuration ceremony. One install and you've got a
full-blooded AI agent — file access, shell commands, git workflow, code review, planning,
sub-agents, the whole outfit. Runs on any LLM that supports tool calling: Anthropic, OpenAI,
AWS Bedrock, Google, Ollama, and a dozen others.

Built on the [Bog Agents SDK](https://github.com/bogware/bog-agents) and [LangGraph](https://github.com/langchain-ai/langgraph). MIT licensed.

[![PyPI](https://img.shields.io/pypi/v/bog-agents-cli)](https://pypi.org/project/bog-agents-cli/)
[![License](https://img.shields.io/pypi/l/bog-agents-cli)](https://opensource.org/licenses/MIT)
[![Downloads](https://img.shields.io/pepy/dt/bog-agents-cli)](https://pypistats.org/packages/bog-agents-cli)

---

## Install

```bash
pip install bog-agents-cli

# Pick your provider (OpenAI included by default)
pip install 'bog-agents-cli[anthropic]'
pip install 'bog-agents-cli[bedrock]'        # AWS Bedrock
pip install 'bog-agents-cli[ollama]'         # Local, no API key
pip install 'bog-agents-cli[all-providers]'  # Everything
```

Or with `uv`:

```bash
uv tool install 'bog-agents-cli[anthropic]'
```

## First Run

```bash
bog-agents
```

If you've got an API key in your environment or AWS credentials in `~/.aws/`, it picks them up
automatically. No key? The setup wizard walks you through it — 30 seconds and you're riding.

```bash
# Or specify your model explicitly
bog-agents -M claude-sonnet-4-6
bog-agents -M gpt-4o
bog-agents -M ollama:llama3            # No API key needed
bog-agents -M bedrock_converse:anthropic.claude-sonnet-4-6  # AWS credentials
```

Check your setup any time:

```bash
bog-agents --doctor
```

---

## Features

### Interactive TUI

A rich terminal interface with streaming responses, syntax highlighting, inline diffs, and
tool-call approval. Everything happens in the terminal — no browser, no Electron, no nonsense.

### 50+ Slash Commands

Type `/` in the interactive session and the autocomplete shows you everything. Here are the
ones that separate the greenhorns from the trail bosses:

| Command | What It Does |
|---------|-------------|
| `/model` | Switch LLM mid-session — Anthropic, OpenAI, Ollama, anything |
| `/plan` | Read-only plan mode. Agent sees the lay of the land without touching a thing |
| `/effort` | Set reasoning depth: `low` (fast), `medium`, `high`, `max` (thorough) |
| `/review` | Code review on staged changes, a commit, or specific files |
| `/test` | Run tests with coverage analysis and generate test skeletons |
| `/pr` | Create, list, or review pull requests without leaving the session |
| `/diff` | Show pending file changes as unified diffs |
| `/undo` | Revert the last file change (git-checkpoint backed) |
| `/compact` | Compress conversation context (`aggressive`, `moderate`, or custom rules) |
| `/cost` | Real-time token usage, cost estimate, and budget enforcement |
| `/context` | Show context window usage with breakdown |
| `/teach` | Teach the agent a workflow — it learns and saves it as a reusable skill |
| `/remember` | Persist insights to agent memory (survives across sessions) |
| `/agent` | Spawn and manage parallel agent threads |
| `/worktree` | Isolated git worktrees for parallel work streams |
| `/record` | Record a session for replay and debugging |
| `/replay` | Play back a recorded session step by step |
| `/branch` | Fork the conversation to explore alternatives |
| `/doctor` | Health check — Python, packages, API keys, tools, sandbox support |
| `/threads` | Browse and resume previous conversations |
| `/recommend` | AI-powered code review with persona-based analysis |
| `/onboard` | Interactive codebase tour for getting up to speed |
| `/health` | Codebase health score — complexity, coverage, quality |
| `/resolve` | AI-assisted merge conflict resolution |
| `/changelog` | Generate a changelog from git history |
| `/infra` | Generate Docker, Kubernetes, or Terraform configs |
| `/audit` | Audit dependencies for known vulnerabilities |
| `/mcp` | Show active MCP servers and available tools |
| `/extensions` | Install and manage extensions |
| `/keybindings` | Customize keyboard shortcuts |
| `/remote` | Submit a task for cloud execution |
| `/profile` | Switch configuration presets |
| `/session` | Show session info, name the session |
| `/clear` | Start a fresh thread |
| `/quit` | Hang up your hat |

### Non-Interactive Mode

This is where automation lives. One command, one task, exit code tells the story.

```bash
# Basic task — no shell access by default
bog-agents -n 'Summarize the README'

# Grant shell access (safe defaults)
bog-agents -n 'Run the test suite' --shell-allow-list recommended

# Specific commands only
bog-agents -n 'Search logs for errors' --shell-allow-list cat,grep,find

# Full shell access
bog-agents -n 'Fix the failing tests and commit' --shell-allow-list all

# Clean output for piping
bog-agents -p 'Explain this code' < my_file.py

# Pipe to another command
bog-agents -p 'Write a code review' < pr_diff.patch | tee review.md

# Machine-readable JSON
bog-agents -n 'List all TODO comments' --json

# No streaming (buffer full response)
bog-agents -n 'Refactor the auth module' --no-stream

# Fix an issue and open a PR in one shot
bog-agents -n 'Fix issue #42' --pr --shell-allow-list all

# Create a draft PR against a specific branch
bog-agents -n 'Add dark mode' --pr --pr-base develop --pr-draft --shell-allow-list all
```

**Exit codes:** `0` success, `1` error, `130` interrupted.

**Shell access in non-interactive mode** is off by default — you grant it explicitly:
- `--shell-allow-list recommended` — curated safe commands (`ls`, `cat`, `grep`, `find`, `wc`, etc.)
- `--shell-allow-list ls,cat,grep` — your own allow-list
- `--shell-allow-list all` — unrestricted shell (use in trusted environments)

### Conversation Resume

Pick up where you left off. Every conversation is a thread with full history.

```bash
bog-agents -r              # Resume most recent thread
bog-agents -r abc123       # Resume a specific thread
bog-agents threads list    # See all threads
bog-agents threads delete abc123  # Clean up
```

### Persistent Memory

The agent remembers things across sessions. Use `/remember` to persist insights, or let the
agent learn naturally. Memory is stored per-agent in `~/.bog-agents/`.

### Custom Skills

Extend the agent with your own slash commands. Skills are Python scripts with a
`SKILL.md` manifest.

```bash
bog-agents skills list           # See installed skills
bog-agents skills create         # Scaffold a new skill
bog-agents skills info my-skill  # Show skill details
bog-agents skills delete my-skill
```

### Named Agents

Run multiple agents with separate memory, prompts, and thread history.

```bash
bog-agents -a researcher    # Use the "researcher" agent
bog-agents -a reviewer      # Use the "reviewer" agent
bog-agents list             # See all agents
bog-agents reset --agent researcher  # Reset an agent's prompt
```

### Remote Sandboxes

Run code in isolated environments when you don't want the agent touching your local files.

```bash
bog-agents --sandbox modal           # Modal serverless sandbox
bog-agents --sandbox daytona         # Daytona cloud sandbox
bog-agents --sandbox runloop         # Runloop sandbox
bog-agents --sandbox-id existing-id  # Reuse an existing sandbox
```

### MCP (Model Context Protocol)

Load external tools via MCP servers. Auto-discovers `.mcp.json` in your project, or specify
a config file.

```bash
bog-agents --mcp-config ./my-mcp-servers.json
bog-agents --no-mcp                  # Disable MCP entirely
bog-agents --trust-project-mcp       # Skip the approval prompt
```

### HTTP API Server

Serve the agent as an HTTP API for integration with other tools.

```bash
bog-agents --serve                           # localhost:8420
bog-agents --serve --serve-host 0.0.0.0 --serve-port 9000
```

### ACP Server

Run as an Agent Client Protocol server (for Zed editor integration).

```bash
bog-agents --acp
```

---

## Model Configuration

### Auto-Detection

The CLI checks for credentials in this order:
1. `[models].default` in `~/.bog-agents/config.toml`
2. `[models].recent` (last `/model` switch)
3. `ANTHROPIC_API_KEY` env var
4. `OPENAI_API_KEY` env var
5. AWS Bedrock (`~/.aws/credentials`, `AWS_ACCESS_KEY_ID`, `AWS_PROFILE`)
6. `GOOGLE_API_KEY` env var
7. `GOOGLE_CLOUD_PROJECT` (Vertex AI)
8. `NVIDIA_API_KEY` env var
9. Ollama (checks if `ollama` binary exists)
10. Interactive setup wizard (if nothing found)

### Setting a Default

```bash
bog-agents --default-model anthropic:claude-sonnet-4-6
bog-agents --default-model                       # Show current default
bog-agents --clear-default-model                 # Remove default
```

### Configuration File

Advanced configuration lives in `~/.bog-agents/config.toml`:

```toml
[models]
default = "anthropic:claude-sonnet-4-6"

[providers.anthropic]
temperature = 0.7
max_tokens = 8192

[providers.openai]
api_base = "https://my-proxy.example.com/v1"
```

### Runtime Model Parameters

```bash
bog-agents -M gpt-4o --model-params '{"temperature": 0.2, "max_tokens": 4096}'
bog-agents -M claude-sonnet-4-6 --profile-override '{"max_input_tokens": 100000}'
```

---

## Supported Providers

Any LangChain-compatible chat model works. Use `provider:model` format.

| Provider | Install Extra | Example |
|----------|--------------|---------|
| Anthropic | `anthropic` | `anthropic:claude-sonnet-4-6` |
| OpenAI | *(included)* | `openai:gpt-4o` |
| AWS Bedrock | `bedrock` | `bedrock_converse:anthropic.claude-sonnet-4-6` |
| Google AI | `google-genai` | `google_genai:gemini-2.5-pro` |
| Vertex AI | `vertexai` | `google_vertexai:gemini-2.5-pro` |
| Ollama | `ollama` | `ollama:llama3` |
| Groq | `groq` | `groq:llama-3.3-70b` |
| DeepSeek | `deepseek` | `deepseek:deepseek-chat` |
| Fireworks | `fireworks` | `fireworks:llama-v3p3-70b` |
| Mistral | `mistralai` | `mistralai:mistral-large` |
| NVIDIA | `nvidia` | `nvidia:nemotron-70b` |
| OpenRouter | `openrouter` | `openrouter:meta-llama/llama-3` |
| Perplexity | `perplexity` | `perplexity:sonar-pro` |
| xAI | `xai` | `xai:grok-2` |
| Cohere | `cohere` | `cohere:command-r-plus` |
| Together | *(via litellm)* | `litellm:together/llama-3-70b` |
| HuggingFace | `huggingface` | `huggingface:meta-llama/Llama-3` |
| Azure OpenAI | *(via openai)* | `azure_openai:gpt-4o` |

---

## CI/CD & Scripting Recipes

```bash
# Code review in CI
git diff main...HEAD | bog-agents -p 'Review this diff for bugs and style issues'

# Generate commit messages
bog-agents -p 'Write a conventional commit message for the staged changes' \
  --shell-allow-list git

# Automated refactoring
bog-agents -n 'Rename getUserData to fetch_user_data across the codebase' \
  --shell-allow-list recommended

# Documentation generation
bog-agents -n 'Generate docstrings for all public functions in src/' \
  --shell-allow-list recommended

# Security audit
bog-agents -n 'Audit this repo for security vulnerabilities' \
  --shell-allow-list recommended --json

# Fix and PR in one shot (great for issue bots)
bog-agents -n 'Fix issue #123' --pr --shell-allow-list all
```

---

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `AWS_ACCESS_KEY_ID` / `AWS_PROFILE` | AWS Bedrock credentials |
| `GOOGLE_API_KEY` | Google AI API key |
| `GOOGLE_CLOUD_PROJECT` | Vertex AI project |
| `NVIDIA_API_KEY` | NVIDIA API key |
| `TAVILY_API_KEY` | Tavily web search |
| `BOG_AGENTS_SHELL_ALLOW_LIST` | Default shell allow-list |
| `BOG_AGENTS_LANGSMITH_PROJECT` | LangSmith tracing project |

Keys can also be set in `.env` (project-level) or `~/.bog-agents/.env` (user-level).

---

## Full CLI Reference

```
bog-agents [OPTIONS] [COMMAND]

Commands:
  list                          List available agents
  reset                         Reset an agent's prompt
  skills                        Manage skills (list/create/info/delete)
  threads                       Manage threads (list/delete)

Core:
  -M, --model MODEL             Model to use
  -a, --agent NAME              Agent name (default: agent)
  -r, --resume [ID]             Resume a thread
  -m, --message TEXT            Auto-submit prompt on start
  --auto-approve                Auto-approve tool calls
  --doctor                      Run diagnostics
  -v, --version                 Show versions
  -h, --help                    Show help

Non-Interactive:
  -n, --non-interactive MSG     Run task and exit
  -p, --print TEXT              Clean output mode (-n + -q)
  -q, --quiet                   Suppress UI chrome
  --no-stream                   Buffer response
  --json                        JSON output
  --shell-allow-list CMDS       Shell access control
  --pr                          Create PR from output
  --pr-base BRANCH              PR base branch
  --pr-draft                    Draft PR

Model:
  --model-params JSON           Extra model kwargs
  --profile-override JSON       Override profile fields
  --default-model [MODEL]       Set/show default model
  --clear-default-model         Clear default

Sandbox:
  --sandbox TYPE                Sandbox provider
  --sandbox-id ID               Reuse existing sandbox
  --sandbox-setup PATH          Setup script

MCP:
  --mcp-config PATH             MCP config file
  --no-mcp                      Disable MCP
  --trust-project-mcp           Trust project MCP

Server:
  --serve                       HTTP API mode
  --serve-host HOST             API host
  --serve-port PORT             API port
  --acp                         ACP server mode
```

---

## Requirements

- Python 3.11+
- At least one LLM provider (API key or local model)

---

## Contributing

We're open to contributions. See [CONTRIBUTING.md](https://github.com/bogware/bog-agents/blob/main/CONTRIBUTING.md).

## License

MIT

---

*The trail's marked. Saddle up.*
