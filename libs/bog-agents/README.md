# Bog Agents

> *Pass through in harmony. Opinionated where it matters.*

The Python SDK underneath [`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/) and
[`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/) — and an
SDK in its own right when you want to build agents that aren't a CLI.

One `create_agent()` call gets you a compiled LangGraph agent with file tools, a shell,
git, sub-agents, plan mode, auto-quality checks, retry-with-backoff against transient
provider failures, and ~80 composable middlewares. Pluggable backends. Tool bundles
for callers who don't want middleware overhead. Any tool-calling LLM. The defaults
are deliberate — you ship something that works on day one without writing scaffolding.

[![PyPI](https://img.shields.io/pypi/v/bog-agents)](https://pypi.org/project/bog-agents/)
[![Python](https://img.shields.io/pypi/pyversions/bog-agents)](https://pypi.org/project/bog-agents/)
[![License](https://img.shields.io/pypi/l/bog-agents)](https://opensource.org/licenses/MIT)
[![Downloads](https://img.shields.io/pepy/dt/bog-agents)](https://pypistats.org/packages/bog-agents)

---

## Philosophy

Most agent frameworks make you assemble the kit. We don't. Bog Agents starts you
with a working agent and lets you peel away or bolt on layers as you understand
what you actually need.

- **Patient by default.** Failures retry with bounded backoff. Hung commands time out.
  Provider hiccups don't kill the run.
- **Opinionated where it matters.** Secure-by-default backends, a memory-only secrets
  vault, structured logging at every chokepoint, panic dumps on uncaught exceptions.
- **No ceremony.** `create_agent()` returns a compiled `CompiledStateGraph` you can
  invoke. Plug it into your app. Done.
- **Composable.** ~80 middlewares snap on or off. Subagents nest. Backends swap. The
  framework gets out of your way. New in 0.8.6: **tool bundles** —
  free-function factories that return `list[BaseTool]` for callers who
  only want a set of tools without the middleware machinery.

The bog is calm, deep, and unhurried. So is the agent.

---

## Install

```bash
pip install bog-agents
```

Provider extras as needed:

```bash
pip install "bog-agents[anthropic]"      # Claude
pip install "bog-agents[openai]"         # GPT
pip install "bog-agents[bedrock]"        # AWS Bedrock
pip install "bog-agents[google-genai]"   # Gemini
pip install "bog-agents[ollama]"         # local models
```

Or all of them: `pip install "bog-agents[all-providers]"`.

---

## 30-second Quick Start

```python
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    system_prompt="You are a careful, concise software engineer.",
)

result = await agent.ainvoke({
    "messages": [{"role": "user", "content": "List Python files in this repo."}]
})

print(result["messages"][-1].content)
```

That gets you: filesystem tools, shell execution, sub-agents, plan-mode, summarization
middleware, prompt caching for Anthropic models, and the standard tool-call patcher.
No additional setup.

### With more knobs

```python
from bog_agents import create_agent, FeatureConfig
from bog_agents.middleware import (
    GitToolsMiddleware,
    MemoryMiddleware,
    ProviderRetryMiddleware,
    SkillsMiddleware,
)

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    config=FeatureConfig(
        enable_audit_trail=True,
        enable_cost_tracking=True,
        budget_usd=5.0,
    ),
    middleware=[
        ProviderRetryMiddleware(max_attempts=3),
        GitToolsMiddleware(),
        MemoryMiddleware(sources=["./AGENTS.md"]),
        SkillsMiddleware(sources=["./skills"]),
    ],
)
```

---

## What's in the box

### Backends

Pluggable filesystems and shells. Pick one or compose them.

| Backend | Use when |
|---|---|
| `StateBackend` (default) | Agent reads / writes happen in graph state. Great for sandboxed tests. |
| `FilesystemBackend` | Real filesystem. Path traversal blocked by `virtual_mode=True` (the default since 0.8.0). |
| `LocalShellBackend` | Filesystem + shell execution on the host. UTF-8 stdout decoding, configurable timeouts, `stdin=/dev/null` so interactive prompts can't hang the agent. |
| `CompositeBackend` | Route different path prefixes to different backends. |
| `SandboxBackend` | Modal / Daytona / RunLoop / LangSmith remote sandboxes. |

### Middlewares (selected)

- **`ProviderRetryMiddleware`** — bounded exponential backoff with jitter on transient
  provider errors (5xx, timeouts, connection resets). Never retries tool calls.
- **`MemoryMiddleware`** — load `AGENTS.md` files into the system prompt. 64 KiB cap;
  `</agent_memory>` close-tags neutralized to prevent prompt-injection forgery.
- **`SkillsMiddleware`** — bundle reusable agent skills with metadata.
- **`GitToolsMiddleware`** — git status / log / diff / blame; opt-in commit / push.
- **`SubAgentMiddleware`** — recursive task decomposition with typed subagent specs.
- **`PlanModeMiddleware`** — structured plan-then-execute flow.
- **`SummarizationMiddleware`** — token-aware history compression.
- **`AnthropicPromptCachingMiddleware`** — automatic prompt-cache breakpoints.
- **`HumanInTheLoopMiddleware`** — pause-and-confirm for risky tool calls.
- **`AuditTrailMiddleware`** — structured records of every agent decision.
- **`RBACMiddleware`** / **`DLPMiddleware`** — access control + data-loss prevention.

Plus 60+ more for cost tracking, citations, hooks, MCP tools, parallel
worktrees, hot-reload skills, browser automation, compliance auditing, and
more. Browse them under `bog_agents.middleware`. **Ordering is
load-bearing** — the canonical sequence is locked by
`tests/unit_tests/test_middleware_canonical_order.py`; touch it
deliberately.

### Tool bundles (alternative to middleware for tool-only features)

```python
from bog_agents import create_agent
from bog_agents.tools import git_tools_bundle

agent = create_agent(
    model="anthropic:claude-sonnet-4-7",
    tools=[*git_tools_bundle(working_dir=".")],
)
```

A bundle is a free function that returns `list[BaseTool]`. No middleware
class to construct, no wrap-stack overhead. Bundles available:
`git_tools_bundle`, `multi_edit_tool`, `read_many_files_tool`. The
corresponding middleware classes (`GitToolsMiddleware`, etc.) are kept
as thin backwards-compatible shims that delegate to the bundles.

### Providers

| Provider | Extra | Notes |
|---|---|---|
| Anthropic | `anthropic` | Default. Claude 4.6 / 4.7 with prompt caching. |
| OpenAI | `openai` | Responses API by default. |
| AWS Bedrock | `bedrock` | Claude / Llama / Titan via `bedrock:` prefix. |
| Google | `google-genai` | Gemini family. |
| Mistral | `mistralai` | |
| Groq | `groq` | |
| DeepSeek | `deepseek` | |
| Fireworks | `fireworks` | |
| Baseten | `baseten` | |
| xAI | `xai` | |
| Ollama | `ollama` | Local models. |

Pass `model="provider:model-id"` and `create_agent` does the rest.

---

## Async first, sync if you want it

```python
# Async — recommended
result = await agent.ainvoke({"messages": [...]})

# Sync — works fine too
result = agent.invoke({"messages": [...]})
```

Streaming is supported via the standard LangGraph stream APIs.

---

## What's new since 0.8.0

- **0.8.7 (Wave X)** — `merge_worktree` ref-injection fix,
  `start_preview_server` shlex parsing + interactive-command DEVNULL,
  daemon `JobRun.dispatch_errors` per-target failure capture.
- **0.8.6 (Wave W)** — **Tool bundles** (`bog_agents.tools.bundles`),
  canonical middleware-ordering test that locks `graph.py`'s
  sequence, `AuditTrailMiddleware.strict_hooks` flag + hook-failure
  counter, fixed server log handle leak on `Popen` failure.
- **0.8.5 (Wave V)** — Stub middleware cleanup: 17 modules deleted,
  ~7,900 lines net deletion.

## What's new in 0.8.0

- **`ProviderRetryMiddleware`** — bounded retries on transient provider failures.
- **`virtual_mode=True` is now the default** for `FilesystemBackend` (and
  `LocalShellBackend`). Path traversal blocked unless explicitly opted out.
- **Subprocess `stdin=/dev/null`** in `LocalShellBackend.execute()` — interactive
  commands like Windows `date` get an immediate EOF instead of hanging.
- **Typed subagent validation** at `create_agent()` — typo'd `name` /
  `description` / `system_prompt` fails fast instead of surfacing as a
  `KeyError` later.
- **`FileOperationError` Literal** extended with `parent_not_found`.
- **`MemoryMiddleware`** caps each AGENTS.md source at 64 KiB and neutralizes
  `</agent_memory>` close-tags (prompt-injection defense).

See [`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)
for the full release history.

---

## When to use this vs. the CLI

- **Use the SDK** when you're embedding an agent in a Python application,
  building your own UI, writing tests, or composing agents into a larger
  system.
- **Use [`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/)** when
  you want a coding agent in your terminal *right now* with no Python wiring.
- **Use [`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/)**
  when you want agents that wake themselves on cron / file changes / webhooks
  / git pushes.

---

## Documentation

- **Full docs**: <https://github.com/bogware/bog-agents/tree/main/docs>
  — [SDK quickstart](https://github.com/bogware/bog-agents/blob/main/docs/sdk/quickstart.md),
  [middleware guide](https://github.com/bogware/bog-agents/blob/main/docs/sdk/middleware.md),
  [tool bundles](https://github.com/bogware/bog-agents/blob/main/docs/sdk/tool-bundles.md),
  [security model](https://github.com/bogware/bog-agents/blob/main/docs/security.md)
- Architecture overview: [`CLAUDE.md`](https://github.com/bogware/bog-agents/blob/main/CLAUDE.md)
- Middleware index: read the docstrings under `bog_agents.middleware`
- Repo: <https://github.com/bogware/bog-agents>
- Issues: <https://github.com/bogware/bog-agents/issues>

---

## License

MIT. See [LICENSE](https://github.com/bogware/bog-agents/blob/main/LICENSE).

*Pass through in harmony.*
