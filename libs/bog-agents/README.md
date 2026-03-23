# 🧠🤖 Bog Agents

[![PyPI - Version](https://img.shields.io/pypi/v/bog-agents?label=%20)](https://pypi.org/project/bog-agents/#history)
[![PyPI - License](https://img.shields.io/pypi/l/bog-agents)](https://opensource.org/licenses/MIT)
[![PyPI - Downloads](https://img.shields.io/pepy/dt/bog-agents)](https://pypistats.org/packages/bog-agents)
[![Twitter](https://img.shields.io/twitter/url/https/twitter.com/langchain.svg?style=social&label=Follow%20%40LangChain)](https://x.com/langchain)

Looking for the JS/TS version? Check out [Bog Agents.js](https://github.com/langchain-ai/bog-agentsjs).

To help you ship LangChain apps to production faster, check out [LangSmith](https://smith.langchain.com).
LangSmith is a unified developer platform for building, testing, and monitoring LLM applications.

## Quick Install

```bash
pip install bog-agents
# or
uv add bog-agents
```

## Getting Started

1. **Set your API key** (the default model uses Anthropic):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or for OpenAI, Google, etc.:

```bash
export OPENAI_API_KEY="sk-..."
export GOOGLE_API_KEY="AI..."
```

2. **Create and run an agent**:

```python
from bog_agents import create_agent

# Uses Claude Sonnet by default (needs ANTHROPIC_API_KEY)
agent = create_agent()

# Or specify a different provider
agent = create_agent(model="openai:gpt-4o")
agent = create_agent(model="google_genai:gemini-2.0-flash")

# Run the agent
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Hello!"}]},
    config={"configurable": {"thread_id": "my-thread"}},
)
```

3. **Run as an HTTP server** (optional):

```bash
pip install 'bog-agents[serve]'
```

```python
from bog_agents import create_agent
from bog_agents.serve import AgentServer

agent = create_agent()
server = AgentServer(agent)
server.run()  # Starts on http://127.0.0.1:8420
```

## 🤔 What is this?

Using an LLM to call tools in a loop is the simplest form of an agent. This architecture, however, can yield agents that are "shallow" and fail to plan and act over longer, more complex tasks.

Applications like "Deep Research", "Manus", and "Claude Code" have gotten around this limitation by implementing a combination of four things: a **planning tool**, **sub agents**, access to a **file system**, and a **detailed prompt**.

`bog-agents` is a Python package that implements these in a general purpose way so that you can easily create a Bog Agents for your application. For a full overview and quickstart of Bog Agents, the best resource is our [docs](https://docs.langchain.com/oss/python/bog-agents/overview).

**Acknowledgements: This project was primarily inspired by Claude Code, and initially was largely an attempt to see what made Claude Code general purpose, and make it even more so.**

## 🚀 Features

### Core Agent
- **LangGraph-powered** — composable agent with middleware architecture
- **Multi-model** — works with Anthropic, OpenAI, Google, DeepSeek, and any LangChain-compatible LLM
- **Sub-agents** — spawn isolated sub-agents for complex tasks
- **File system** — read, write, edit, search files with pluggable backends
- **Planning** — built-in todo list and plan mode for structured task execution

### Middleware (Pluggable)

| Middleware | Description |
|-----------|-------------|
| `FilesystemMiddleware` | File read/write/edit/search + multi-edit and batch read |
| `GitToolsMiddleware` | 9 git tools: status, diff, log, commit, add, branch, stash, blame, show |
| `RepoMapMiddleware` | Structural code map with symbol extraction (Python, JS, TS, Rust, Go, Java) |
| `CheckpointingMiddleware` | Git-based snapshots before mutations, with undo/diff |
| `CostTrackerMiddleware` | Token usage, cost estimation, budget enforcement, effort levels |
| `PlanModeMiddleware` | Read-only mode that blocks mutating tools |
| `AutoQualityMiddleware` | Auto-lint/test after edits with project detection |
| `ArchitectMiddleware` | Dual-model architect/reviewer with cross-model consultation |
| `ParallelAgentsMiddleware` | Concurrent sub-agent task execution |
| `LifecycleHooksMiddleware` | 15 event types for external tool integration |
| `ContextPackingMiddleware` | Smart structured context compression |
| `SummarizationMiddleware` | Auto-summarization when context window fills |
| `MemoryMiddleware` | Persistent AGENTS.md memory across sessions |
| `SkillsMiddleware` | Custom skill/instruction loading |
| `SafeToolsConfig` | Per-tool auto-approval rules |

### Security
- **OS-level sandbox** — bubblewrap (Linux), seatbelt (macOS), landlock isolation
- **Human-in-the-loop** — configurable tool approval with interrupt handling

## 📦 Quick Start

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

# Run the agent
result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Fix the failing tests"}]},
    config={"configurable": {"thread_id": "my-session"}},
)
```

## 📖 Resources

- **[Documentation](https://docs.langchain.com/oss/python/bog-agents)** — Full documentation
- **[API Reference](https://reference.langchain.com/python/bog-agents/)** — Full SDK reference documentation
- **[Chat LangChain](https://chat.langchain.com)** - Chat interactively with the docs

## 📕 Releases & Versioning

See our [Releases](https://docs.langchain.com/oss/python/release-policy) and [Versioning](https://docs.langchain.com/oss/python/versioning) policies.

## 💁 Contributing

As an open-source project in a rapidly developing field, we are extremely open to contributions, whether it be in the form of a new feature, improved infrastructure, or better documentation.

For detailed information on how to contribute, see the [Contributing Guide](https://docs.langchain.com/oss/python/contributing/overview).
