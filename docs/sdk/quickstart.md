# SDK Quickstart

> Embed an agent in your Python app. Five lines for the happy path.
> Forty lines when you want it shaped to your project.

## Install

```bash
pip install bog-agents
pip install 'bog-agents[anthropic]'      # Claude
pip install 'bog-agents[all-providers]'  # everything
```

`bog-agents` (SDK) is the foundation. `bog-agents-cli` and
`bog-agents-daemon` both depend on it. Install just the SDK when
you're embedding in your own app and don't want the TUI / daemon
infrastructure.

## Happy path

```python
from bog_agents import create_agent

agent = create_agent(model="anthropic:claude-opus-4-7")

result = await agent.ainvoke({
    "messages": [{"role": "user", "content": "List Python files in this repo."}]
})

print(result["messages"][-1].content)
```

That gets you, for free:

- Filesystem tools (read / write / edit / list / glob)
- Shell execution (with HITL approval, allow-list, timeout)
- Sub-agent delegation (`task` tool)
- Plan-mode (think, then act)
- Summarization (auto-compress old turns when context fills)
- Prompt caching (Anthropic-specific, automatic)
- Patch-tool-calls (recovers from dangling tool messages)

You did not configure any of those. The defaults are the defaults
for a reason.

## With more knobs

```python
from bog_agents import create_agent, FeatureConfig
from bog_agents.middleware import (
    GitToolsMiddleware,
    MemoryMiddleware,
    ProviderRetryMiddleware,
    SkillsMiddleware,
)

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    system_prompt="You are a careful, concise software engineer.",
    config=FeatureConfig(
        enable_audit_trail=True,
        enable_cost_tracking=True,
        budget_usd=5.0,            # surface an approval prompt past $5
        enable_plan_mode=True,
        enable_checkpointing=True,  # git-snapshot before every file mutation
    ),
    middleware=[
        ProviderRetryMiddleware(max_attempts=3),
        GitToolsMiddleware(),
        MemoryMiddleware(sources=["./AGENTS.md"]),
        SkillsMiddleware(sources=["./skills"]),
    ],
)
```

The order in `middleware=[...]` matters. The canonical sequence is
locked by `tests/unit_tests/test_middleware_canonical_order.py`. See
[middleware.md](middleware.md) for the why.

## Sync vs async

```python
# Async (recommended)
result = await agent.ainvoke({"messages": [...]})

# Sync (works fine)
result = agent.invoke({"messages": [...]})

# Streaming (LangGraph's stream API)
async for event in agent.astream({"messages": [...]}, stream_mode="updates"):
    print(event)
```

## Picking a model

| Spec | Notes |
|---|---|
| `anthropic:claude-opus-4-7` | Flagship. 1M context. Most reliable for code. |
| `anthropic:claude-sonnet-4-6` | Cheaper, fast, 1M context. |
| `anthropic:claude-haiku-4-5` | Cheapest Claude. 200K context. |
| `openai:gpt-5` | OpenAI flagship. Responses API. |
| `bedrock_converse:anthropic.claude-sonnet-4-20250514-v1:0` | Bedrock-hosted Claude (your AWS creds, your region). |
| `google_genai:gemini-2.5-pro` | Google. 2M context. |
| `ollama:llama3` | Local. No API key needed. |
| `xai:grok-4` | xAI. |
| `bog_agents` looks at the provider prefix. No prefix means auto-detect from the model name. | |

The full list comes from `langchain.chat_models.init_chat_model`
plus the CLI's `model_config.PROVIDER_API_KEY_ENV` map. New providers
the upstream library adds are picked up automatically.

## Backends

The default agent uses `LocalShellBackend` — your real filesystem +
your real shell, both sandboxed to the working directory by default.
Swap to something else with the `backend=` kwarg:

```python
from bog_agents.backends import StateBackend, CompositeBackend

# Pure in-memory — great for tests
agent = create_agent(model=..., backend=StateBackend())

# Route different paths to different backends
agent = create_agent(model=..., backend=CompositeBackend({
    "/sandbox": StateBackend(),
    "/host":    LocalShellBackend(root_dir="/safe/path"),
}))
```

See [backends.md](backends.md) for the full backend story.

## Sandboxes

When you want shell execution to run *outside* the host:

```python
from bog_agents.backends import DaytonaBackend, ModalBackend

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    backend=DaytonaBackend(api_key=...),   # remote Daytona workspace
)
```

Same agent, same tools, the shell now runs in an isolated workspace.
Useful when the model might generate destructive commands and you
want a real barrier.

## Tools without middleware

For tool-only contributions, prefer the bundle pattern over building
a one-off middleware:

```python
from bog_agents import create_agent
from bog_agents.tools import git_tools_bundle

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    tools=[*git_tools_bundle(working_dir=".")],
)
```

`git_tools_bundle`, `multi_edit_tool`, `read_many_files_tool` are
the exposed bundles today. Full pattern in
[tool-bundles.md](tool-bundles.md).

## Sub-agents

```python
from bog_agents import create_agent, SubAgent

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    subagents=[
        SubAgent(
            name="researcher",
            description="Reads the web and large documents",
            system_prompt="You research and summarize. No code edits.",
            model="anthropic:claude-haiku-4-5",   # cheaper
            tools=["web_fetch", "read_file"],
        ),
        SubAgent(
            name="implementer",
            description="Edits files and runs tests",
            system_prompt="You implement what's been planned. No web access.",
        ),
    ],
)
```

The main agent gets a `task()` tool that delegates to a named
sub-agent. Sub-agents inherit the parent's default middleware stack
plus anything you pass in their `middleware=` field.

For parallel fan-out across sub-agents, see
`ParallelAgentMiddleware` and `WorktreeMiddleware`.

## HITL (Human-in-the-Loop)

The default `interrupt_on` list covers risky tools (file writes,
shell execution, web fetch). Per-tool customization:

```python
from langchain.agents.middleware import InterruptOnConfig

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    interrupt_on={
        "shell_execute": InterruptOnConfig(),                  # always approve
        "write_file": InterruptOnConfig(when_args={"path": "secrets/*"}),  # only for secrets/
    },
)
```

Receive interrupts via the LangGraph stream:

```python
from langgraph.types import Command, Interrupt

async for event in agent.astream({"messages": [...]}):
    for interrupt in event.get("__interrupt__", []):
        # Show the tool call to the user, get their approval
        decision = await ask_user(interrupt.value)
        if decision == "approve":
            # Resume the graph
            await agent.ainvoke(Command(resume={"type": "approve"}), config=...)
```

The CLI's approval menu is one implementation of this pattern; you
can write your own.

## Saving + resuming sessions

```python
from langgraph.checkpoint.sqlite import SqliteSaver

with SqliteSaver.from_conn_string("agent.sqlite") as checkpointer:
    agent = create_agent(
        model="anthropic:claude-opus-4-7",
        checkpointer=checkpointer,
    )

    config = {"configurable": {"thread_id": "user-42"}}
    result = await agent.ainvoke({"messages": [...]}, config=config)
```

Same `thread_id` across calls resumes the conversation. Different
`thread_id`s are isolated.

## Common pitfalls

### "My agent doesn't have the git tool"

You didn't enable `enable_git_tools=True` in `FeatureConfig`, or pass
`GitToolsMiddleware()` in `middleware=`. The bundles + middleware are
opt-in. Default agent has filesystem + shell + sub-agents only.

### "My custom model's `bind_tools` raises NotImplementedError"

`BaseChatModel.bind_tools` is abstract. Even response-only fakes
need an override. The simplest:

```python
def bind_tools(self, tools, *, tool_choice=None, **kwargs):
    return self
```

See `bog_agents.drive.replay_model.FakeChatModel` for the canonical
no-op shape.

### "Middleware order matters more than I expected"

It does. See [middleware.md](middleware.md). The canonical sequence
is locked by tests so future contributors don't shuffle it by
accident — but if you're rolling your own list, the rules are:

- `CostTrackerMiddleware` before `SummarizationMiddleware` (so it
  counts pre-compression tokens).
- `SummarizationMiddleware` before `AnthropicPromptCachingMiddleware`
  (so cache keys reflect post-summary state).
- `MemoryMiddleware` last (so it sees the final message list).
- HITL middleware last (so it intercepts the agent's actual tool
  calls).

## Next steps

- [Middleware](middleware.md) — write your own
- [Tool bundles](tool-bundles.md) — the leaner alternative for tool-only contributions
- [Backends](backends.md) — filesystem / shell / sandbox internals
- [Cookbook](../cookbook.md) — task-shaped recipes

---

*One function call. Then layers as you want them. Not a function
call per layer.*
