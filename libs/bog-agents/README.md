# Bog Agents

> *Pass through in harmony. Opinionated where it matters.*

The Python SDK underneath [`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/) and
[`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/) — and an
SDK in its own right when you want to build agents that aren't a CLI.

One `create_agent()` call gets you a compiled LangGraph agent with file tools, a shell,
git, sub-agents, plan mode, auto-quality checks, retry-with-backoff against transient
provider failures, and 90+ composable middlewares. Pluggable backends. Tool bundles
for callers who don't want middleware overhead. Drop-in
[deepagents](https://github.com/langchain-ai/deepagents) compatibility. Any
tool-calling LLM. The defaults are deliberate — you ship something that works on
day one without writing scaffolding.

[![PyPI](https://img.shields.io/pypi/v/bog-agents)](https://pypi.org/project/bog-agents/)
[![Python](https://img.shields.io/pypi/pyversions/bog-agents)](https://pypi.org/project/bog-agents/)
[![License](https://img.shields.io/pypi/l/bog-agents)](https://opensource.org/licenses/MIT)
[![Downloads](https://img.shields.io/pepy/dt/bog-agents)](https://pypistats.org/packages/bog-agents)

---

## Philosophy

A careful hand beats a fast one. Most agent frameworks make you assemble the
kit. We don't. Bog Agents starts you with a working agent and lets you peel
away or bolt on layers as you understand what the job actually asks for.

- **Patient by default.** Failures retry with bounded backoff. Hung commands time out.
  Provider hiccups don't kill the run.
- **Opinionated where it matters.** Secure-by-default backends, a memory-only secrets
  vault, structured logging at every chokepoint, panic dumps on uncaught exceptions.
- **No ceremony.** `create_agent()` returns a compiled `CompiledStateGraph` you can
  invoke. Plug it into your app. Done.
- **Composable.** 90+ middlewares snap on or off. Subagents nest. Backends swap. The
  framework gets out of your way. **Tool bundles** — free-function factories
  that return `list[BaseTool]` — serve callers who only want a set of tools
  without the middleware machinery.

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

## deepagents compatibility

Coming from [deepagents](https://github.com/langchain-ai/deepagents)? You can
switch over without rewriting — and switch back if you ever want to. We ship a
compatibility surface that speaks the same dialect.

```python
from bog_agents import create_deep_agent, DeepAgentState, FilesystemPermission

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[...],
    # Confine the agent's filesystem reach with allow/deny/interrupt rules
    permissions=[
        FilesystemPermission(operations=["write", "delete"], paths=["./src/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["./secrets/**"], mode="deny"),
    ],
)
```

What's in the box:

| Symbol | What it gives you |
|---|---|
| `create_deep_agent` | `create_agent` with `state_schema=DeepAgentState` defaulted on. |
| `DeepAgentState` | The deepagents state shape, backed by a `DeltaChannel` messages reducer (O(N) checkpoints, not O(N²)). |
| `FilesystemPermission` | Per-operation, per-path `allow` / `deny` / `interrupt` rules. `deny` is enforced in `wrap_tool_call`; `interrupt` routes through human-in-the-loop. |
| `RubricMiddleware` | The grader self-evaluation loop — score the agent's own output against a rubric and retry. |
| `HarnessProfile` / `HarnessProfileConfig` | Per-`provider:model` overlays: prompt, extra middleware, tool-description overrides, excluded tools/middleware, general-purpose subagent. |
| `ProviderProfile` | Per-provider `init_kwargs` / `pre_init` / `init_kwargs_factory`, applied during model resolution. |
| `register_harness_profile` / `register_provider_profile` | Register your own profiles. |

Permissions and a typed `response_format` are also accepted on `SubAgent`
specs, so sub-agents can be sandboxed independently of their parent. Every
addition is opt-in: existing `create_agent` callers see no behavior change.

---

## What's in the box

### Backends

Pluggable filesystems and shells. Pick one or compose them.

| Backend | Use when |
|---|---|
| `StateBackend` (default) | Agent reads / writes happen in graph state. Great for sandboxed tests. |
| `FilesystemBackend` | Real filesystem. Path traversal blocked by `virtual_mode=True` (the default). |
| `LocalShellBackend` | Filesystem + shell execution on the host. UTF-8 stdout decoding, configurable timeouts, `stdin=/dev/null` so interactive prompts can't hang the agent. |
| `CompositeBackend` | Route different path prefixes to different backends. |
| `SandboxBackend` | Daytona / Modal / RunLoop / LangSmith remote sandboxes. |

### Middlewares (selected)

- **`ProviderRetryMiddleware`** — bounded exponential backoff with jitter on transient
  provider errors (5xx, timeouts, connection resets). Never retries tool calls.
- **`FilesystemPermissionsMiddleware`** — enforce `FilesystemPermission` rules
  (allow / deny / interrupt) on file tools.
- **`RubricMiddleware`** — grade the agent's output against a rubric and loop.
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
    model="anthropic:claude-sonnet-4-6",
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
| Anthropic | `anthropic` | Default. Claude 4.x with prompt caching. |
| OpenAI | `openai` | Responses API by default. |
| AWS Bedrock | `bedrock` | Claude / Llama / Titan via `bedrock:` prefix. Auto inference-profile resolution + SSO refresh. |
| Google | `google-genai` | Gemini family. |
| Mistral | `mistralai` | |
| Groq | `groq` | |
| DeepSeek | `deepseek` | |
| Fireworks | `fireworks` | |
| Baseten | `baseten` | |
| xAI | `xai` | |
| Ollama | `ollama` | Local models. |

Pass `model="provider:model-id"` and `create_agent` does the rest.
Per-provider initialization can be tuned with a `ProviderProfile`.

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

## What's new in 0.9.x

- **0.10** — two new first-class primitives + safer middleware ordering:
  - **`bog_agents.evals`** — evaluation as an importable primitive: `Dataset`,
    rule-based + LLM-as-judge `Scorer`s, `run_evals(...)`, and
    `EvalReport.assert_pass_rate()` to gate releases in CI.
  - **`bog_agents.guardrails`** — composable input/output guardrails with
    fail-fast tripwire semantics (`Blocklist` / `MaxLength` / `NoSecrets` /
    `LLMGuardrail`), plus `create_agent(guardrails=[...])`.
  - A declarative **`.bog-agents/sandbox.toml`** (preinstall / runner size /
    snapshot / network egress allowlist) loader.
  - Correctness fixes from a fresh SPE audit (see `REVIEW.md`): `MemoryMiddleware`
    now runs before prompt caching (was defeating the cache) and `DLPMiddleware`
    before `AuditTrailMiddleware` (was logging unredacted values).
- **0.9.4** — **deepagents parity**: `create_deep_agent`, `DeepAgentState`
  (with an O(N) `DeltaChannel` messages reducer), `FilesystemPermission`,
  `RubricMiddleware`, `HarnessProfile` / `ProviderProfile`, plus
  `SubAgent.permissions` / `response_format`. Provider resilience
  live-tested across Anthropic, AWS Bedrock, and OpenAI; `bedrock` /
  `openai` / `all-providers` extras added.
- **0.9.1** — **Bedrock, seamless**: automatic inference-profile resolution
  and auto SSO-credential refresh in `resolve_model`.
- **0.9.0** — scriptable TUI groundwork, compliance auditing, repo-wide
  security sweep.
- **0.8.6** — **tool bundles** (`bog_agents.tools.bundles`) and the
  canonical middleware-ordering test that locks `graph.py`'s sequence.

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
