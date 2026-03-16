# 🧠🤖 Bog Agents CLI

[![PyPI - Version](https://img.shields.io/pypi/v/bog-agents-cli?label=%20)](https://pypi.org/project/bog-agents-cli/#history)
[![PyPI - License](https://img.shields.io/pypi/l/bog-agents-cli)](https://opensource.org/licenses/MIT)
[![PyPI - Downloads](https://img.shields.io/pepy/dt/bog-agents-cli)](https://pypistats.org/packages/bog-agents-cli)
[![Twitter](https://img.shields.io/twitter/url/https/twitter.com/langchain.svg?style=social&label=Follow%20%40LangChain)](https://x.com/langchain)

<p align="center">
  <img src="https://raw.githubusercontent.com/langchain-ai/bog-agents/main/libs/cli/images/cli.png" alt="Bog Agents CLI" width="600"/>
</p>

## Quick Install

```bash
curl -LsSf https://raw.githubusercontent.com/langchain-ai/bog-agents/main/libs/cli/scripts/install.sh | bash
```

```bash
# With model provider extras (OpenAI is included by default)
BOG_AGENTS_EXTRAS="anthropic,groq" curl -LsSf https://raw.githubusercontent.com/langchain-ai/bog-agents/main/libs/cli/scripts/install.sh | bash
```

Or install directly with `uv`:

```bash
# Install with chosen model providers (OpenAI is included by default)
uv tool install 'bog-agents-cli[anthropic,groq]'
```

Run the CLI:

```bash
bog-agents
```

## 🤔 What is this?

The fastest way to start using Bog Agents. `bog-agents-cli` is a pre-built coding agent in your terminal — similar to Claude Code or Cursor — powered by any LLM that supports tool calling. One install command and you're up and running, no code required.

**What the CLI adds on top of the SDK:**

- **Interactive TUI** — rich terminal interface with streaming responses
- **Conversation resume** — pick up where you left off across sessions
- **Web search** — multi-provider search (Tavily, Serper, SearXNG)
- **Remote sandboxes** — run code in isolated environments (Modal, Runloop, Daytona, & more)
- **Persistent memory** — agent remembers context across conversations
- **Custom skills** — extend the agent with your own slash commands
- **Headless mode** — run non-interactively for scripting and CI
- **Human-in-the-loop** — approve or reject tool calls before execution

### New in this release

- **Git workflow** — built-in git tools (status, diff, log, commit, blame) and auto-checkpointing with undo
- **Code review** — `/review` command for structured code review on staged changes, commits, or files
- **Cost tracking** — real-time token usage, cost estimation, and budget enforcement (`/cost`, `/context`)
- **Plan mode** — read-only mode that blocks mutations for safe exploration (`/plan`)
- **Effort levels** — control reasoning depth: low/medium/high/max (`/effort`)
- **Configuration profiles** — named presets for different workflows (`/profile review|refactor|debug|quick|careful`)
- **Selective compaction** — control what stays in context (`/compact aggressive|moderate|minimal`)
- **Streaming diffs** — real-time unified diff preview for file edits (`/diff`, `/undo`)
- **Extensions** — plugin system with manifest-based install/uninstall (`/extensions`)
- **Session forking** — branch conversations to explore alternatives
- **Session replay** — record and replay workflows (`/record`, `/replay`)
- **Teaching mode** — teach the agent new skills from your actions (`/teach`)
- **Shell shortcuts** — `!command` for quick shell escape, `#note` for quick memory
- **Custom keybindings** — configure key bindings via `keybindings.json` (`/keybindings`)
- **Health diagnostics** — verify your environment is correctly set up (`/doctor`)
- **JSON output** — structured JSON and stream-JSON modes for CI/scripting (`--output json`)
- **Remote execution** — submit tasks to LangGraph Cloud (`/remote`)
- **OAuth for MCP** — OAuth 2.0 + PKCE for remote MCP server authentication
- **Lifecycle hooks** — 15 event types for external integrations (tool calls, file ops, shell, etc.)

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/clear` | Clear chat and start new thread |
| `/compact` | Summarize conversation (supports `aggressive`, `moderate`, custom rules) |
| `/context` | Show context window usage |
| `/cost` | Show session cost and token usage |
| `/diff` | Show pending file changes as unified diff |
| `/doctor` | Run health check diagnostics |
| `/effort` | Set effort level (low/medium/high/max) |
| `/extensions` | Manage extensions (list/install/uninstall) |
| `/keybindings` | Show or customize key bindings |
| `/model` | Switch model mid-session |
| `/plan` | Toggle read-only plan mode |
| `/profile` | Switch configuration profile |
| `/record` | Start/stop recording session for replay |
| `/remember` | Update memory from conversation |
| `/remote` | Submit task for cloud execution |
| `/replay` | Replay a recorded session |
| `/review` | Code review on staged changes or files |
| `/teach` | Start teaching mode to learn a workflow |
| `/threads` | Browse and resume previous threads |
| `/tokens` | Token usage |
| `/undo` | Undo last file change (git checkpoint) |

## 📖 Resources

- **[CLI Documentation](https://docs.langchain.com/oss/python/bog-agents/cli/overview)**
- **[Changelog](https://github.com/langchain-ai/bog-agents/blob/main/libs/cli/CHANGELOG.md)**
- **[Source code](https://github.com/langchain-ai/bog-agents/tree/main/libs/cli)**
- **[Bog Agents SDK](https://github.com/langchain-ai/bog-agents)** — underlying agent harness

## 📕 Releases & Versioning

See our [Releases](https://docs.langchain.com/oss/python/release-policy) and [Versioning](https://docs.langchain.com/oss/python/versioning) policies.

## 💁 Contributing

As an open-source project in a rapidly developing field, we are extremely open to contributions, whether it be in the form of a new feature, improved infrastructure, or better documentation.

For detailed information on how to contribute, see the [Contributing Guide](https://docs.langchain.com/oss/python/contributing/overview).

## 🤝 Acknowledgements

This project was primarily inspired by Claude Code, and initially was largely an attempt to see what made Claude Code general purpose, and make it even more so.
