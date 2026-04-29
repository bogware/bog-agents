# Bog Agents CLI

A coding agent that lives in your terminal. Point it at the work, step back, let it run.

No scaffolding. No config ceremony. One install and you've got file access, a shell,
git, code review, planning, sub-agents — the whole outfit. Works with any LLM that does
tool calls: Anthropic, OpenAI, Bedrock, Google, Ollama, and a dozen others.

Built on the [Bog Agents SDK](https://github.com/bogware/bog-agents) and
[LangGraph](https://github.com/langchain-ai/langgraph). MIT.

[![PyPI](https://img.shields.io/pypi/v/bog-agents-cli)](https://pypi.org/project/bog-agents-cli/)
[![License](https://img.shields.io/pypi/l/bog-agents-cli)](https://opensource.org/licenses/MIT)
[![Downloads](https://img.shields.io/pepy/dt/bog-agents-cli)](https://pypistats.org/packages/bog-agents-cli)

---

## Install

```bash
pip install bog-agents-cli

pip install 'bog-agents-cli[anthropic]'      # Claude
pip install 'bog-agents-cli[bedrock]'        # AWS Bedrock
pip install 'bog-agents-cli[ollama]'         # Local models, no key
pip install 'bog-agents-cli[all-providers]'  # Everything
```

Or with `uv`:

```bash
uv tool install 'bog-agents-cli[anthropic]'
```

## First run

```bash
bog-agents
```

If there's a key in your environment — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, AWS creds,
anything — it finds them and gets moving. No key, no problem: the setup wizard handles
the introductions in about thirty seconds.

```bash
bog-agents -M claude-sonnet-4-6
bog-agents -M gpt-4o
bog-agents -M ollama:llama3              # local, free
bog-agents -M bedrock_converse:anthropic.claude-sonnet-4-6
```

Something feeling off? Ask it.

```bash
bog-agents --doctor
```

---

## What it does

**Runs in a real TUI.** Streaming tokens, syntax highlighting, inline diffs, approve
tools one-by-one or not at all. Terminal only — no browser, no Electron, no nonsense.

**Keeps state between runs.** Every session is a thread you can come back to. Memory,
summaries, labels, and per-project context persist in `~/.bog-agents/`.

**Scripts cleanly.** `-n`, `-p`, `--json`, `--no-stream`, and proper exit codes make it
a tool you can pipe, cron, and drop into CI without regret.

**Separates concerns.** Named agents each get their own prompt, memory, skills, and
thread history. A `researcher`, a `reviewer`, a `debugger` — all on the same install.

**Scales out.** Remote sandboxes for isolated work. MCP for external tools. An HTTP
server mode when something else needs to drive.

---

## Slash commands

Hit `/` in an interactive session and autocomplete shows you everything. The commands
that carry the most weight:

| Command | What it does |
|---------|-------------|
| `/model` | Switch LLM mid-session |
| `/plan` | Read-only mode. Agent scouts the territory without touching anything |
| `/effort` | Reasoning depth: `low`, `medium`, `high`, `max` |
| `/review` | Review staged changes, a commit, or specific files |
| `/diff` | Show pending file changes as unified diffs |
| `/compact` | Trim conversation context when it gets heavy |
| `/cost` | Token usage, cost estimate, budget |
| `/context` | Context-window usage with a breakdown |
| `/remember` | Persist an insight to agent memory across sessions |
| `/agent` | Spawn and manage parallel agent threads |
| `/background` | Queue local work and watch it from the side |
| `/dashboard` | Live multi-agent snapshot |
| `/worktree` | Isolated git worktrees for parallel streams |
| `/resume` | Resume latest, specific, or tagged threads |
| `/threads` | Browse and manage past conversations |
| `/recommend` | Persona-based code review |
| `/onboard` | Walk a new codebase with you |
| `/mcp` | Show active MCP servers and tools |
| `/plugin` | Install, list, enable, disable extensions |
| `/remote` | Submit, track, and stop remote tasks |
| `/doctor` | Health check: Python, packages, keys, tools, sandboxes |
| `/profile` | Switch configuration presets |
| `/session` | Label, tag, summarize, and export a thread |
| `/keybindings` | Show bindings or the config path |
| `/clear` | Start a fresh thread |
| `/quit` | Hang up your hat |

---

## Non-interactive mode

Where the automation lives. One command, one task, exit code tells the story.

```bash
# Basic task — no shell, safe by default
bog-agents -n 'Summarize the README'

# Grant shell access with a curated allow-list
bog-agents -n 'Run the test suite' --shell-allow-list recommended

# Specific commands only
bog-agents -n 'Search logs for errors' --shell-allow-list cat,grep,find

# Unrestricted shell — trusted environments only
bog-agents -n 'Fix the failing tests and commit' --shell-allow-list all

# Clean output for piping
bog-agents -p 'Explain this code' < my_file.py
bog-agents -p 'Write a code review' < pr_diff.patch | tee review.md

# Machine-readable
bog-agents -n 'List all TODO comments' --json

# Fix an issue and open a PR in one shot
bog-agents -n 'Fix issue #42' --pr --shell-allow-list all

# Draft PR against a specific branch
bog-agents -n 'Add dark mode' --pr --pr-base develop --pr-draft --shell-allow-list all
```

**Exit codes:** `0` success, `1` error, `130` interrupted.

**Shell access** is off by default. Three ways to turn it on:
- `--shell-allow-list recommended` — curated safe commands (`ls`, `cat`, `grep`, `find`, `wc`, more)
- `--shell-allow-list ls,cat,grep` — roll your own
- `--shell-allow-list all` — no guardrails

---

## Threads and memory

Come back to what you were working on.

```bash
bog-agents -r              # Latest thread
bog-agents -r abc123       # Specific thread
bog-agents threads list    # See 'em all
bog-agents threads delete abc123
```

Persistent memory lives in `~/.bog-agents/<agent>/AGENTS.md`. Use `/remember` to add
a note the agent should carry forward. Use `/session` to attach labels, tags, project
names, and summaries to the current thread so you can find it later.

Project-level memory lives in `.bog-agents/AGENTS.md` at your repo root — check it in,
and every teammate on this codebase gets the same context when they fire up the CLI.

---

## Skills and extensions

Teach the agent something once, reuse it forever. A skill is a `SKILL.md` manifest plus
whatever scripts and prompts it needs. Extensions bundle skills and slash commands together.

```bash
bog-agents skills list
bog-agents skills create              # Scaffold a new skill
bog-agents skills info my-skill
bog-agents skills delete my-skill
```

In the TUI:

```text
/plugin install <path-or-url>
/plugin info <name>
/plugin enable <name>
/plugin disable <name>
```

---

## Named agents

Run separate agents with separate memory, prompts, and history. Same install, different
hats.

```bash
bog-agents -a researcher
bog-agents -a reviewer
bog-agents list                           # All agents
bog-agents reset --agent researcher       # Back to default prompt
```

---

## Remote sandboxes

When the work's too rough for the local machine, or you want it to run somewhere else
while you get on with yours.

```bash
bog-agents --sandbox modal                # Modal serverless
bog-agents --sandbox daytona              # Daytona cloud
bog-agents --sandbox runloop              # Runloop
bog-agents --sandbox-id existing-id       # Hop back on an existing sandbox
```

Inside the TUI, `/remote` queues tracked tasks:

```text
/remote config
/remote submit --label scout --branch-prefix fix "investigate the failing tests"
/remote status <id>
/remote stop <id>
```

---

## MCP (Model Context Protocol)

External tools, loaded on demand. The CLI auto-finds `.mcp.json` in your project, or
you can point at one.

```bash
bog-agents --mcp-config ./my-mcp-servers.json
bog-agents --no-mcp                       # Off
bog-agents --trust-project-mcp            # Skip the approval prompt
```

---

## Server modes

Put the agent behind an HTTP API when another tool wants to drive.

```bash
bog-agents --serve                                    # localhost:8420
bog-agents --serve --serve-host 0.0.0.0 --serve-port 9000
```

Or run as an Agent Client Protocol server, for Zed:

```bash
bog-agents --acp
```

---

## Model configuration

### Detection order

No `-M` flag? The CLI looks for credentials in this order and picks the first it finds:

1. `[models].default` in `~/.bog-agents/config.toml`
2. `[models].recent` (last `/model` switch)
3. `ANTHROPIC_API_KEY`
4. `OPENAI_API_KEY`
5. AWS Bedrock (`~/.aws/credentials`, `AWS_ACCESS_KEY_ID`, `AWS_PROFILE`)
6. `GOOGLE_API_KEY`
7. `GOOGLE_CLOUD_PROJECT` (Vertex AI)
8. `NVIDIA_API_KEY`
9. Ollama (if the `ollama` binary is on PATH)
10. Setup wizard (if nothing found)

### Setting a default

```bash
bog-agents --default-model anthropic:claude-sonnet-4-6
bog-agents --default-model                    # Show current
bog-agents --clear-default-model              # Remove
```

### Config file

Advanced knobs live in `~/.bog-agents/config.toml`:

```toml
[models]
default = "anthropic:claude-sonnet-4-6"

[providers.anthropic]
temperature = 0.7
max_tokens = 8192

[providers.openai]
api_base = "https://my-proxy.example.com/v1"
```

### Runtime overrides

```bash
bog-agents -M gpt-4o --model-params '{"temperature": 0.2, "max_tokens": 4096}'
bog-agents -M claude-sonnet-4-6 --profile-override '{"max_input_tokens": 100000}'
```

---

## Providers

Use `provider:model` format. Any LangChain-compatible chat model works.

| Provider | Extra | Example |
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

### Local Ollama: which model to use

Ollama's chat API mimics OpenAI's tools-API JSON schema. Models trained
against that exact schema engage tools cleanly; models trained against
other formats (Mistral's `[TOOL_CALLS]{}`, Hermes' `<tool_call>{}</tool_call>`,
Qwen's chat-template tool call) emit calls in the message text and Ollama's
adapter doesn't translate them. The CLI ships a parser middleware that
recovers most text-shaped tool calls automatically when you select an
`ollama:` model, but recovery is best-effort.

- **Recommended**: `ollama:gpt-oss:20b` — OpenAI tools-API native, works
  end-to-end with no recovery needed. Fits in 16GB of VRAM.
- **Recovers via parser**: `ollama:mistral-nemo:12b`, `ollama:hermes3:8b`,
  some `ollama:qwen2.5-coder` runs.
- **Doesn't work**: `ollama:deepseek-coder-v2:16b` (Ollama's manifest
  doesn't expose the `tools` capability — see
  [ollama/ollama#3303](https://github.com/ollama/ollama/issues) if you want
  to nudge upstream), `ollama:starcoder2`, `ollama:codellama`.

Run `bog-agents --doctor` to see whether your configured default Ollama
model is on the known-good list.

---

## Recipes for CI and scripting

```bash
# Code review in CI
git diff main...HEAD | bog-agents -p 'Review this diff for bugs and style issues'

# Commit message from staged changes
bog-agents -p 'Write a conventional commit message for the staged changes' \
  --shell-allow-list git

# Automated refactor
bog-agents -n 'Rename getUserData to fetch_user_data across the codebase' \
  --shell-allow-list recommended

# Docstring pass
bog-agents -n 'Generate docstrings for all public functions in src/' \
  --shell-allow-list recommended

# Security audit, JSON out
bog-agents -n 'Audit this repo for security vulnerabilities' \
  --shell-allow-list recommended --json

# Issue bot: fix and open a PR
bog-agents -n 'Fix issue #123' --pr --shell-allow-list all
```

---

## Environment variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `AWS_ACCESS_KEY_ID` / `AWS_PROFILE` | AWS Bedrock |
| `GOOGLE_API_KEY` | Google AI |
| `GOOGLE_CLOUD_PROJECT` | Vertex AI |
| `NVIDIA_API_KEY` | NVIDIA |
| `TAVILY_API_KEY` | Tavily web search |
| `BOG_AGENTS_SHELL_ALLOW_LIST` | Default shell allow-list |
| `BOG_AGENTS_LANGSMITH_PROJECT` | LangSmith tracing project |

Keys can also sit in a project-level `.env` or a user-level `~/.bog-agents/.env`.

---

## CLI reference

```
bog-agents [OPTIONS] [COMMAND]

Commands:
  list                          List agents
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
- At least one LLM provider (key or local model)

## Contributing

See [CONTRIBUTING.md](https://github.com/bogware/bog-agents/blob/main/CONTRIBUTING.md).

## License

MIT.

---

*The trail's marked. Saddle up.*
