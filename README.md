# Bog Agents

An agent harness. You point it at a problem, it gets to work.

No wiring up prompts, tools, or context by hand. You get a working agent out of the box — planning, file access, shell, sub-agents, the whole outfit. Customize what needs customizing. Leave the rest alone.

Built on [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview). MIT licensed. Works with any LLM that can call tools.

## Install the SDK

```bash
pip install bog-agents
```

```python
from bog_agents import create_agent

agent = create_agent()
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Research LangGraph and write a summary"}]}
)
```

That's a running agent. It can plan, read and write files, run shell commands, and spin up sub-agents when the job calls for it.

## Make It Yours

Swap the model. Add tools. Change the prompt. The agent doesn't care — it'll use what you give it.

```python
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[my_custom_tool],
    system_prompt="You are a research assistant.",
    enable_git_tools=True,
    enable_cost_tracking=True,
)

result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Fix the failing tests"}]},
    config={"configurable": {"thread_id": "my-session"}},
)
```

`create_agent` returns a compiled LangGraph graph. Streaming, Studio, checkpointers — anything LangGraph does, this does too. MCP works through [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters).

## The CLI

A terminal agent that handles its own business. Web search, remote sandboxes, persistent memory, human-in-the-loop approval.

```bash
pip install bog-agents-cli

# Pick your provider
pip install 'bog-agents-cli[anthropic]'
pip install 'bog-agents-cli[ollama]'       # no API key needed
pip install 'bog-agents-cli[all]'          # everything
```

```bash
# Set a key and go
export ANTHROPIC_API_KEY="sk-ant-..."
bog-agents

# Or pick a model
bog-agents -M claude-sonnet-4-6
bog-agents -M gpt-4o
bog-agents -M ollama:llama3
```

```bash
# Check your setup
bog-agents --doctor
```

See the [CLI README](libs/cli/) for the full rundown.

## Run from Source

```bash
git clone https://github.com/bogware/bog-agents.git
cd bog-agents

# SDK
cd libs/bog-agents && uv sync && cd ../..

# CLI (links to local SDK)
cd libs/cli && uv sync && cd ../..

# Run it
cd libs/cli && uv run bog-agents
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

## Run the Tests

```bash
# SDK
cd libs/bog-agents && make test

# CLI
cd libs/cli && make test

# Single file
uv run --group test pytest tests/unit_tests/test_specific.py

# Lint everything
make lint
```

## What's in the Box

**Tools out of the gate:** `read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`, `execute`, `write_todos`, `task`

**Middleware** — plug in what you need, leave out what you don't:

| Middleware | What It Does |
|-----------|-------------|
| `FilesystemMiddleware` | File operations, multi-edit, batch read |
| `GitToolsMiddleware` | status, diff, log, commit, add, branch, stash, blame, show |
| `RepoMapMiddleware` | Structural code map — Python, JS, TS, Rust, Go, Java |
| `CheckpointingMiddleware` | Git-based snapshots before mutations, with undo |
| `CostTrackerMiddleware` | Token usage, cost, budget enforcement |
| `PlanModeMiddleware` | Read-only mode, blocks mutating tools |
| `AutoQualityMiddleware` | Auto-lint and test after edits |
| `ArchitectMiddleware` | Dual-model architect/reviewer |
| `ParallelAgentsMiddleware` | Concurrent sub-agents |
| `SummarizationMiddleware` | Auto-summarize when context fills up |
| `MemoryMiddleware` | Persistent AGENTS.md memory across sessions |
| `SkillsMiddleware` | Custom skill and instruction loading |

## Monorepo Layout

```
libs/
├── bog-agents/        # Core SDK
├── cli/               # Terminal UI
├── acp/               # Agent Client Protocol (Zed)
├── harbor/            # Evaluation framework
├── vscode-extension/  # VS Code extension
└── partners/          # Sandbox integrations
    ├── daytona/       #   Daytona cloud sandboxes
    ├── modal/         #   Modal serverless
    ├── runloop/       #   Runloop sandboxes
    └── quickjs/       #   QuickJS JS sandbox
```

## Providers

Any LangChain-compatible chat model works. Use `provider:model` format.

| Provider | Install | Example |
|----------|---------|---------|
| Anthropic | `bog-agents-cli[anthropic]` | `anthropic:claude-sonnet-4-6` |
| OpenAI | *(included)* | `openai:gpt-4o` |
| Ollama | `bog-agents-cli[ollama]` | `ollama:llama3` |
| Google | `bog-agents-cli[google-genai]` | `google_genai:gemini-2.5-pro` |
| DeepSeek | `bog-agents-cli[deepseek]` | `deepseek:deepseek-chat` |
| Groq | `bog-agents-cli[groq]` | `groq:llama-3.3-70b` |
| AWS Bedrock | `bog-agents-cli[bedrock]` | `bedrock_converse:anthropic.claude-sonnet-4-6` |
| Fireworks | `bog-agents-cli[fireworks]` | `fireworks:llama-v3p3-70b` |
| Mistral | `bog-agents-cli[mistralai]` | `mistralai:mistral-large` |
| NVIDIA | `bog-agents-cli[nvidia]` | `nvidia:nemotron-70b` |
| OpenRouter | `bog-agents-cli[openrouter]` | `openrouter:meta-llama/llama-3` |
| Perplexity | `bog-agents-cli[perplexity]` | `perplexity:sonar-pro` |
| xAI | `bog-agents-cli[xai]` | `xai:grok-2` |
| LiteLLM | `bog-agents-cli[litellm]` | `litellm:gpt-4o` |
| HuggingFace | `bog-agents-cli[huggingface]` | `huggingface:meta-llama/Llama-3` |

## Security

Bog Agents trusts the LLM to do its job. Boundaries are enforced at the tool and sandbox level — not by expecting the model to hold its own reins. Bubblewrap on Linux, Seatbelt on macOS, Landlock where available.

## Documentation

- [Examples](examples/)
- [Publishing](PUBLISHING.md)
- [Contributing](CONTRIBUTING.md)

## Acknowledgements

This project drew its first breath from studying Claude Code — figuring out what made it general purpose, then pushing that further.

---

*The trail's marked. Saddle up.*
