# Bog Agents

> *Pass through in harmony. Opinionated where it matters.*

The Python SDK underneath [`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/) and
[`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/) — and an
SDK in its own right when you want to build agents that aren't a CLI.

One `create_agent()` call gets you a compiled LangGraph agent with file tools, a
shell, git, sub-agents, plan mode, auto-quality checks, retry-with-backoff, and
~90 composable middlewares. Then it goes where few frameworks do: **governed
autonomy as importable primitives** — agent teams, hard cost caps, proof-of-work
evidence, and a findings ledger — so you can build agents you trust to run
unattended. Pluggable backends. Tool bundles for callers who don't want
middleware overhead. Drop-in
[deepagents](https://github.com/langchain-ai/deepagents) compatibility. Any
tool-calling LLM.

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
- **Trustworthy when unwatched.** Cost caps, evidence bundles, a hash-chained
  action log, and a findings ledger are first-class, not add-ons.
- **No ceremony.** `create_agent()` returns a compiled `CompiledStateGraph` you can
  invoke. Plug it into your app. Done.
- **Composable.** ~90 middlewares snap on or off. Sub-agents nest. Backends swap.
  **Tool bundles** — free-function factories that return `list[BaseTool]` — serve
  callers who only want a set of tools without the middleware machinery.

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

Other extras: `pip install "bog-agents[pdf]"` enables the `read_file` tool to
extract text from `.pdf` files, and `pip install "bog-agents[serve]"` exposes
the agent over HTTP.

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

That gets you: filesystem tools, shell execution, sub-agents, plan-mode,
summarization middleware, prompt caching for Anthropic models, and the standard
tool-call patcher. No additional setup.

### With more knobs

```python
from bog_agents import create_agent, FeatureConfig
from bog_agents.middleware import GitToolsMiddleware, MemoryMiddleware, SkillsMiddleware

agent = create_agent(
    model="anthropic:claude-sonnet-4-6",
    config=FeatureConfig(
        enable_action_log=True,     # hash-chained, signable audit trail
        enable_cost_tracking=True,
        budget_usd=5.0,             # pauses at the cap instead of crashing
    ),
    middleware=[
        GitToolsMiddleware(),
        MemoryMiddleware(sources=["./AGENTS.md"]),
        SkillsMiddleware(sources=["./skills"]),
    ],
)
```

`FeatureConfig` is the toggle surface — a single dataclass that replaces what
would otherwise be a 100+ parameter call. New optional features get a field here,
not a new keyword argument.

---

## Governed-autonomy primitives

The parts that let you build agents you can trust to run without a human
watching. All are pure, dependency-light, and injectable — they unit-test
without a live model.

### Teams, cost caps, and evidence

```python
from bog_agents.teams import TaskLedger, run_team
from bog_agents.cost_ledger import CostLedger, RunawayCaps
from bog_agents.evidence import EvidenceBundle, collect_git_evidence, render_evidence_markdown

# A shared, atomic, dependency-aware task board; teammates claim what's ready.
ledger = TaskLedger()
ledger.add("write the parser", task_id="parse")
ledger.add("write the tests", depends_on=["parse"], task_id="tests")

# Every spawn and dollar is counted; the run stops at the cap.
cost = CostLedger(caps=RunawayCaps(max_subagents=8, max_cost_usd=5.0))
report = await run_team(ledger, ["worker-1", "worker-2"], teammate_runner=runner, cost_ledger=cost)

# Proof-of-work: the diff, the verify-command output, and the rubric verdict.
bundle = EvidenceBundle(**collect_git_evidence("."))
print(render_evidence_markdown(bundle))   # merge_ready gates on checks + rubric
```

### The findings ledger

```python
from bog_agents.findings_store import FindingsStore, parse_findings_text

store = FindingsStore(".bog-agents/findings.db")
store.record(parse_findings_text(scan_output, source="nightly", run_id="r42"), run_id="r42")

store.gate(max_severity="high")     # → GateResult; .passed is your CI yes/no
store.to_sarif()                    # SARIF 2.1.0 for code-scanning uploads
```

Rows are keyed by a **stable fingerprint** (rule + path + normalised message —
never the line number), so a re-scan updates a finding instead of re-opening it,
a finding that stops appearing auto-closes as `fixed`, and a triage decision
sticks until the code changes enough to change the message.

### Fork sub-agents

```python
from bog_agents.middleware.subagents import SubAgent

# mode="fork" seeds the child with the parent's whole conversation so far —
# a second opinion or a parallel investigation that keeps everything it knows.
spec = SubAgent(name="reviewer", mode="fork", description="adversarially review the change")
```

### Compliance: a tamper-evident action log + OpenTelemetry

```python
from bog_agents.action_log import ActionLog

log = ActionLog(".bog-agents/actions.log", run_id="r42")
log.append("tool_call", name="execute", args={"command": "pytest"})
log.verify()                        # the hash chain is intact
```

`bog_agents.otel_export` emits GenAI-semconv spans over a dependency-free
OTLP/HTTP sink — no provider SDKs required.

---

## deepagents compatibility

Coming from [deepagents](https://github.com/langchain-ai/deepagents)? Switch over
without rewriting — and switch back if you ever want to.

```python
from bog_agents import create_deep_agent, DeepAgentState, FilesystemPermission

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[...],
    permissions=[
        FilesystemPermission(operations=["write", "delete"], paths=["./src/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["./secrets/**"], mode="deny"),
    ],
)
```

| Symbol | What it gives you |
|---|---|
| `create_deep_agent` | `create_agent` with `state_schema=DeepAgentState` defaulted on. |
| `DeepAgentState` | The deepagents state shape, backed by a `DeltaChannel` messages reducer (O(N) checkpoints, not O(N²)). |
| `FilesystemPermission` | Per-operation, per-path `allow` / `deny` / `interrupt` rules. `deny` is enforced in `wrap_tool_call`; `interrupt` routes through human-in-the-loop. |
| `RubricMiddleware` | The grader self-evaluation loop — score the agent's own output against a rubric and retry. |
| `HarnessProfile` / `ProviderProfile` | Per-`provider:model` overlays: prompt, extra middleware, tool-description overrides, excluded tools/middleware. |

Permissions and a typed `response_format` are also accepted on `SubAgent`
specs. Every addition is opt-in: existing `create_agent` callers see no change.

---

## What's in the box

### Backends

| Backend | Use when |
|---|---|
| `StateBackend` (default) | Agent reads / writes happen in graph state. Great for sandboxed tests. |
| `FilesystemBackend` | Real filesystem. Path traversal blocked by `virtual_mode=True` (the default). |
| `LocalShellBackend` | Filesystem + shell on the host. UTF-8 stdout, configurable timeouts, `stdin=/dev/null` so interactive prompts can't hang the agent. Accepts an OS-level `LocalSandbox`. |
| `CompositeBackend` | Route different path prefixes to different backends. |
| `SandboxBackend` | Remote sandboxes. **Daytona** ships as first-party source; other providers plug in via their extras. |

### OS-level sandbox

`LocalShellBackend(sandbox=..., require_sandbox=...)` wraps every shell command in
bubblewrap (Linux) / seatbelt (macOS), with a hard network cut or a
`bog_agents.sandbox.egress_proxy` **localhost allowlist proxy** for bounded
egress. `require_sandbox=True` fails closed where no launcher exists. Driven
declaratively from `.bog-agents/sandbox.toml`.

### Middlewares (selected)

- **`ProviderRetryMiddleware`** / **`ProviderFailoverMiddleware`** — bounded
  backoff on transient errors; rate-limit failover across `[models].fallbacks`.
- **`FilesystemPermissionsMiddleware`** — enforce allow / deny / interrupt rules.
- **`RubricMiddleware`** — grade the agent's output against a rubric and loop.
- **`MemoryMiddleware`** — load `AGENTS.md` files into the system prompt; close-tag
  neutralisation to prevent prompt-injection forgery.
- **`SkillsMiddleware`** — bundle reusable agent skills; symlinked dirs refused by default.
- **`SubAgentMiddleware`** — recursive decomposition; `isolated` and `fork` modes.
- **`CodeModeMiddleware`** — the model writes Python that calls your tools as
  functions in a child interpreter; HITL-gated tools refused, spawns counted, bounded.
- **`SummarizationMiddleware`** / **`AnthropicPromptCachingMiddleware`** —
  token-aware compaction; automatic prompt-cache breakpoints.
- **`ActionLogMiddleware`** / **`OTelExportMiddleware`** — the compliance artefact
  and GenAI-semconv telemetry.
- **`DLPMiddleware`** / **`GuardrailMiddleware`** — data-loss prevention;
  composable input/output tripwires.

Plus ~80 more — cost tracking, citations, hooks, MCP tools, parallel worktrees,
the street-sweeper context pruner, expert-rules engine, and more, under
`bog_agents.middleware`. **Ordering is load-bearing** — the canonical sequence is
locked by `tests/unit_tests/test_middleware_canonical_order.py`.

### Tool bundles

```python
from bog_agents import create_agent
from bog_agents.tools import git_tools_bundle

agent = create_agent(model="anthropic:claude-sonnet-4-6", tools=[*git_tools_bundle(working_dir=".")])
```

A bundle is a free function that returns `list[BaseTool]` — no middleware class,
no wrap-stack overhead. Available: `git_tools_bundle`, `multi_edit_tool`,
`read_many_files_tool`.

### Measured harness overhead

The default per-turn overhead is ~8,979 tokens before your words (system prompt
~1,845 + tool schemas ~7,134, approx tokenizer); the built-in `lean` profile
(`FeatureConfig(harness_profile="lean")`) is ~3,115. `bog_agents.token_audit`
attributes every token to the middleware or tool that added it, and
`tests/unit_tests/smoke_tests/test_harness_overhead.py` fails CI on a regression.

### Providers

| Provider | Extra | Notes |
|---|---|---|
| Anthropic | `anthropic` | Default. Claude 4.x with prompt caching. |
| OpenAI | `openai` | Responses API by default. |
| AWS Bedrock | `bedrock` | Auto inference-profile resolution + SSO refresh. |
| Google | `google-genai` | Gemini family. |
| Mistral / Groq / DeepSeek / Fireworks / Baseten / xAI | respective | |
| Ollama | `ollama` | Local models. |

Pass `model="provider:model-id"` and `create_agent` does the rest.

---

## Async first, sync if you want it

```python
result = await agent.ainvoke({"messages": [...]})   # recommended
result = agent.invoke({"messages": [...]})           # works fine too
```

Streaming is supported via the standard LangGraph stream APIs.

---

## Highlights

Organised by capability; see
[`CHANGELOG.md`](https://github.com/bogware/bog-agents/blob/main/CHANGELOG.md)
for the version history.

- **Governed autonomy** — `bog_agents.teams` (ledger + mailbox + `run_team`),
  `bog_agents.cost_ledger` (`CostLedger` + `RunawayCaps`), `bog_agents.evidence`
  (proof-of-work bundles), `bog_agents.findings_store` (the fingerprinted ledger,
  SARIF, CI gate).
- **Cost certainty** — `bog_agents.spend_ledger` (durable daily $ ledger) and
  `CostTrackerMiddleware(on_budget="interrupt")` that pauses at the cap.
- **Compliance** — `bog_agents.action_log` (hash-chained, signable) and
  `bog_agents.otel_export` (GenAI semconv, dependency-free OTLP/HTTP).
- **Governed code mode** — `middleware/code_mode.py`: the model scripts tool calls
  in a child interpreter, HITL-gated tools refused, spawns counted.
- **Fork sub-agents** — `SubAgent(mode="fork")` seeds a child with the parent's
  conversation.
- **Team file exchange** — typed `Attachment` on `Message`, DLP-scanned, over the
  in-memory or SQLite mailbox.
- **Evals & guardrails** — `bog_agents.evals` (`Dataset`, scorers, `run_evals`,
  `assert_pass_rate`) and `bog_agents.guardrails` (composable tripwires).
- **deepagents parity** — `create_deep_agent`, `DeepAgentState`,
  `FilesystemPermission`, harness/provider profiles.

---

## When to use this vs. the CLI

- **Use the SDK** when you're embedding an agent in a Python application,
  building your own UI, writing tests, or composing agents into a larger system.
- **Use [`bog-agents-cli`](https://pypi.org/project/bog-agents-cli/)** when you
  want a coding agent in your terminal *right now* with no Python wiring.
- **Use [`bog-agents-daemon`](https://pypi.org/project/bog-agents-daemon/)** when
  you want agents that wake themselves on cron / file changes / webhooks / git
  pushes, or scan a repo on a schedule.

---

## Documentation

- **Full docs**: <https://github.com/bogware/bog-agents/tree/main/docs>
  — [SDK quickstart](https://github.com/bogware/bog-agents/blob/main/docs/sdk/quickstart.md),
  [middleware guide](https://github.com/bogware/bog-agents/blob/main/docs/sdk/middleware.md),
  [tool bundles](https://github.com/bogware/bog-agents/blob/main/docs/sdk/tool-bundles.md),
  [governed autonomy](https://github.com/bogware/bog-agents/blob/main/docs/cli/governed-autonomy.md),
  [security model](https://github.com/bogware/bog-agents/blob/main/docs/security.md)
- Architecture overview: [`CLAUDE.md`](https://github.com/bogware/bog-agents/blob/main/CLAUDE.md)
- Repo: <https://github.com/bogware/bog-agents> · Issues:
  <https://github.com/bogware/bog-agents/issues>

---

## License

MIT. See [LICENSE](https://github.com/bogware/bog-agents/blob/main/LICENSE).

*Pass through in harmony.*
