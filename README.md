<div align="center">
  <h3>bog-agents - The deepest swampy cli money can't buy.</h3>
</div>
</div>

<br>

bog-agents is an agent harness. An opinionated, ready-to-run agent out of the box. Instead of wiring up prompts, tools, and context management yourself, you get a working agent immediately and customize what you need.

**What's included:**

- **Planning** — `write_todos` for task breakdown and progress tracking
- **Filesystem** — `read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep` for reading and writing context
- **Shell access** — `execute` for running commands (with sandboxing)
- **Sub-agents** — `task` for delegating work with isolated context windows
- **Smart defaults** — Prompts that teach the model how to use these tools effectively
- **Context management** — Auto-summarization when conversations get long, large outputs saved to files

> [!NOTE]
> Looking for the JS/TS library? Check out [bog_agents.js](https://github.com/langchain-ai/bog-agentsjs).

## Quickstart

```bash
pip install bog-agents
# or
uv add bog-agents
```

```python
from bog_agents import create_agent

agent = create_agent()
result = agent.invoke({"messages": [{"role": "user", "content": "Research LangGraph and write a summary"}]})
```

The agent can plan, read/write files, and manage its own context. Add tools, customize prompts, or swap models as needed.

> [!TIP]
> For developing, debugging, and deploying AI agents and LLM applications, see [LangSmith](https://docs.langchain.com/langsmith/home).

## Customization

Add your own tools, swap models, customize prompts, configure sub-agents, and more. See the [documentation](https://docs.langchain.com/oss/python/bog-agents/overview) for full details.

```python
from langchain.chat_models import init_chat_model

agent = create_agent(
    model=init_chat_model("openai:gpt-4o"),
    tools=[my_custom_tool],
    system_prompt="You are a research assistant.",
)
```

MCP is supported via [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters).

## Bog Agents CLI

<p align="center">
  <img src="libs/cli/images/cli.png" alt="Bog Agents CLI" width="600"/>
</p>

```bash
curl -LsSf https://raw.githubusercontent.com/langchain-ai/bog-agents/main/libs/cli/scripts/install.sh | bash
```

Web search, remote sandboxes, persistent memory, human-in-the-loop approval, and more. See the [CLI README](libs/cli/) for the full feature set.

## LangGraph Native

`create_agent` returns a compiled [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) graph. Use it with streaming, Studio, checkpointers, or any LangGraph feature.

## FAQ

### Why should I use this?

- **100% open source** — MIT licensed, fully extensible
- **Provider agnostic** — Works with any Large Language Model model that supports tool calling, including both frontier and open models
- **Built on LangGraph** — Production-ready runtime with streaming, persistence, and checkpointing
- **Batteries included** — Planning, file access, sub-agents, and context management work out of the box
- **Get started in seconds** — `uv add bog-agents` and you have a working agent
- **Customize in minutes** — Add tools, swap models, tune prompts when you need to

---

## Documentation

- [docs.langchain.com](https://docs.langchain.com/oss/python/bog-agents/overview) – Comprehensive documentation, including conceptual overviews and guides
- [reference.langchain.com/python](https://reference.langchain.com/python/bog-agents/) – API reference docs for Bog Agents packages
- [Chat LangChain](https://chat.langchain.com/) – Chat with the LangChain documentation and get answers to your questions

**Discussions**: Visit the [LangChain Forum](https://forum.langchain.com) to connect with the community and share all of your technical questions, ideas, and feedback.

## Running Locally

### Prerequisites

- Python 3.11+ ([pyenv](https://github.com/pyenv/pyenv) or system Python)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- At least one LLM API key **or** [Ollama](https://ollama.com) for fully local models

### Option 1: Install from PyPI

```bash
# Install the CLI (includes the SDK)
pip install bog-agents-cli

# With a specific provider
pip install 'bog-agents-cli[anthropic]'

# With local Ollama support (no API key needed)
pip install 'bog-agents-cli[ollama]'

# With everything (all providers + sandbox + web search)
pip install 'bog-agents-cli[all]'
```

### Option 2: Run from Source

```bash
# Clone the repo
git clone https://github.com/langchain-ai/bog-agents.git
cd bog-agents

# Install SDK
cd libs/bog-agents && uv sync && cd ../..

# Install CLI (links to local SDK)
cd libs/cli && uv sync && cd ../..

# Run the CLI
cd libs/cli && uv run bog-agents
```

### Using with Ollama (Fully Local, No API Key)

```bash
# Install Ollama: https://ollama.com
ollama pull llama3

# Run with Ollama
bog-agents -M ollama:llama3
```

### Using with Cloud Providers

```bash
# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."

# Run (provider auto-detected from model name)
bog-agents -M claude-sonnet-4-6
bog-agents -M gpt-4o
```

### Using the SDK Programmatically

```python
from bog_agents import create_agent

# Default model (auto-detected from env)
agent = create_agent()

# Specific model
agent = create_agent(model="ollama:llama3")

# With custom tools
agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[my_tool],
    system_prompt="You are a research assistant.",
)

# Run
result = agent.invoke({"messages": [{"role": "user", "content": "Hello!"}]})
```

### Running Tests

```bash
# SDK tests
cd libs/bog-agents && make test

# CLI tests
cd libs/cli && make test

# Lint all packages
make lint  # from repo root
```

### Diagnostics

```bash
# Check your environment
bog-agents --doctor
```

## Monorepo Structure

```
libs/
├── bog-agents/          # Core SDK (create_agent, middleware, backends)
├── cli/                 # Interactive terminal AI assistant (bog-agents-cli)
├── acp/                 # Agent Client Protocol integration (Zed editor)
├── harbor/              # Evaluation/benchmark framework
├── vscode-extension/    # VS Code extension (preview)
└── partners/            # Sandbox integrations
    ├── daytona/         #   Daytona cloud sandboxes
    ├── modal/           #   Modal serverless compute
    ├── runloop/         #   Runloop sandboxes
    └── quickjs/         #   QuickJS JavaScript sandbox
```

## Supported Providers

| Provider | Install | Model Example |
|----------|---------|---------------|
| Anthropic | `pip install 'bog-agents-cli[anthropic]'` | `anthropic:claude-sonnet-4-6` |
| OpenAI | *(included by default)* | `openai:gpt-4o` |
| Ollama (local) | `pip install 'bog-agents-cli[ollama]'` | `ollama:llama3` |
| Google | `pip install 'bog-agents-cli[google-genai]'` | `google_genai:gemini-2.5-pro` |
| DeepSeek | `pip install 'bog-agents-cli[deepseek]'` | `deepseek:deepseek-chat` |
| Groq | `pip install 'bog-agents-cli[groq]'` | `groq:llama-3.3-70b` |
| AWS Bedrock | `pip install 'bog-agents-cli[bedrock]'` | `bedrock:anthropic.claude-v2` |
| Fireworks | `pip install 'bog-agents-cli[fireworks]'` | `fireworks:llama-v3p3-70b` |
| Mistral | `pip install 'bog-agents-cli[mistralai]'` | `mistralai:mistral-large` |
| NVIDIA | `pip install 'bog-agents-cli[nvidia]'` | `nvidia:nemotron-70b` |
| OpenRouter | `pip install 'bog-agents-cli[openrouter]'` | `openrouter:meta-llama/llama-3` |
| Perplexity | `pip install 'bog-agents-cli[perplexity]'` | `perplexity:sonar-pro` |
| xAI | `pip install 'bog-agents-cli[xai]'` | `xai:grok-2` |
| LiteLLM | `pip install 'bog-agents-cli[litellm]'` | `litellm:gpt-4o` |
| HuggingFace | `pip install 'bog-agents-cli[huggingface]'` | `huggingface:meta-llama/Llama-3` |

> Any LangChain-compatible chat model works. Use the `provider:model` format.

## Additional resources

- **[Examples](examples/)** — Working agents and patterns
- [Contributing Guide](https://docs.langchain.com/oss/python/contributing/overview) – Learn how to contribute to LangChain projects and find good first issues.
- [Code of Conduct](https://github.com/langchain-ai/langchain/?tab=coc-ov-file) – Our community guidelines and standards for participation.

---

## Acknowledgements

This project was primarily inspired by Claude Code, and initially was largely an attempt to see what made Claude Code general purpose, and make it even more so.

## Security

Bog Agents follows a "trust the LLM" model. The agent can do anything its tools allow. Enforce boundaries at the tool/sandbox level, not by expecting the model to self-police. See the [security policy](https://github.com/langchain-ai/bog-agents?tab=security-ov-file) for more information.
