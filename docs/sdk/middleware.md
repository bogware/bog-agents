# Middleware

> Middleware wraps every model call. Sometimes also every tool call.
> Use sparingly. Default to a tool bundle when you're only delivering
> tools.

## The two-line picture

Middleware composes left-to-right around the model call. The first
item in the list is *outermost* (sees the request first, sees the
response last). The last item is *innermost* (closest to the model).

```text
TodoListMiddleware                ← outermost (sees request first)
  └── SkillsMiddleware
        └── CostTrackerMiddleware
              └── SummarizationMiddleware
                    └── FilesystemMiddleware
                          └── SubAgentMiddleware
                                └── AnthropicPromptCachingMiddleware    ← innermost
                                      └── (the actual model call)
```

The order matters. `CostTrackerMiddleware` before
`SummarizationMiddleware` so the cost record reflects the
pre-compression token count. `SummarizationMiddleware` before
`AnthropicPromptCachingMiddleware` so the cache key reflects what
actually gets sent. The full canonical order is locked by
`tests/unit_tests/test_middleware_canonical_order.py` — touching it
fails CI.

## When to write a middleware

When you need to:

- **Modify the system prompt every call.** E.g. inject AGENTS.md
  contents into the prompt.
- **Intercept tool calls before they execute.** E.g. plan-mode
  blocking mutations, expert-rules denying calls.
- **Transform messages on their way to the model.** E.g. summarize
  old history, redact secrets, swap in a different model under
  load.
- **Track state across turns.** E.g. cost ledger, audit trail, plan
  step counter.
- **Hook into the agent lifecycle.** E.g. checkpoint git before
  every file write, fire a webhook after each turn.

When **not** to write a middleware:

- You only want to add tools. **Use a [tool bundle](tool-bundles.md)
  instead.** The bundle is a free function returning
  `list[BaseTool]`. No wrap-stack overhead. No order-sensitivity. No
  graph composition burden.
- The behavior is a one-shot system-prompt mutation at agent build
  time. Pass it as a parameter on `create_agent` or compose the
  prompt yourself before calling `create_agent`.

## Anatomy of a middleware

```python
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ResponseT,
)
from typing_extensions import TypedDict


class MyState(TypedDict):
    """State this middleware persists across turns."""
    turn_count: int


class MyMiddleware(AgentMiddleware[MyState, ContextT, ResponseT]):
    """Demonstrates the four hooks middleware can override."""

    state_schema = MyState

    def __init__(self, *, threshold: int = 10) -> None:
        self.threshold = threshold
        self.tools = self._build_tools()    # optional — see below

    def _build_tools(self) -> list[BaseTool]:
        # Contribute tools to the agent. Optional. If you only want
        # tools, prefer a tool bundle (see tool-bundles.md).
        ...
        return [tool1, tool2]

    async def awrap_model_call(self, request, call_next):
        # Wrap every model call. Modify `request` before, inspect
        # `await call_next(request)` after.
        request.messages.append(SystemMessage("Don't forget the README."))
        response = await call_next(request)
        return response

    async def before_tool_call(self, tool_call, state):
        # Hook fired before a tool executes. Return None to allow,
        # or a Command to override (deny, modify, route).
        if tool_call.name == "shell_execute" and "rm -rf" in tool_call.args.get("command", ""):
            return ToolDeny(reason="Refusing to rm -rf via middleware.")
        return None

    async def after_tool_call(self, tool_call, result, state):
        # Hook fired after a tool returns. Inspect the result, log,
        # emit a webhook, update middleware state.
        state["turn_count"] += 1
```

Hooks you can implement (override only what you need):

| Hook | When it fires | Use for |
|---|---|---|
| `awrap_model_call(request, call_next)` | Every model API call | Cost tracking, summarization, prompt injection, retries |
| `wrap_model_call(...)` | Same, sync version | Same |
| `before_tool_call(tool_call, state)` | Before a tool runs | Approvals, plan-mode, expert rules, RBAC |
| `after_tool_call(tool_call, result, state)` | After a tool returns | Audit trail, webhooks, cost annotation |
| `before_agent(state)` | Once at the start of the agent run | Skill loading, AGENTS.md injection, initial setup |
| `after_agent(state)` | Once at the end | Final webhook, telemetry, summary |

Async (`awrap_*`) is preferred. The SDK and the CLI both run in
asyncio.

## State

`state_schema` declares a TypedDict for cross-turn state. The agent
runtime merges your TypedDict into the overall graph state — your
keys can sit next to LangGraph's built-ins. State is per-thread
(via the checkpointer); two threads have independent state.

Read state inside a hook:

```python
async def awrap_model_call(self, request, call_next):
    turn = request.state.get("turn_count", 0)
    if turn > 100:
        # don't bill us into oblivion
        raise RuntimeError("Too many turns; aborting")
    return await call_next(request)
```

Write state via the `runtime.update_state` Command pattern (look at
`bog_agents.middleware.summarization` for the canonical example).

## Required-by-other-middleware

If your middleware depends on another middleware running before it:

```python
class ResultSynthesisMiddleware(AgentMiddleware[...]):
    requires: ClassVar[list[type[AgentMiddleware]]] = [ParallelWorktreeMiddleware]
```

`_validate_middleware_ordering` runs at `create_agent` build time and
raises `ValueError` if `ParallelWorktreeMiddleware` is missing or
appears later in the list.

## Real-world examples

The codebase has ~25 production middleware. Look at these for
patterns:

- **`bog_agents.middleware.cost_tracker.CostTrackerMiddleware`** —
  awrap_model_call with token accounting + budget guard. Threading
  lock for shared instance across parallel agents.
- **`bog_agents.middleware.summarization.SummarizationToolMiddleware`** —
  awrap_model_call with auto-compression. State for tracking
  summarization events. Customizable token thresholds.
- **`bog_agents.middleware.expert_rules.ExpertRulesMiddleware`** —
  before_tool_call with a forward-chaining rule engine that can
  deny, modify, or require approval. State for rule activations.
- **`bog_agents.middleware.checkpointing.CheckpointingMiddleware`** —
  before_tool_call that creates a git stash before each file
  mutation. Pure side-effect, no state schema needed.
- **`bog_agents.middleware.audit_trail.AuditTrailMiddleware`** —
  after-everything hook that records every action. Hook callback
  for durable-store integration. Strict-mode flag for compliance
  contexts.

## Anti-patterns

### "A middleware to just add a tool"

```python
class MyToolMiddleware(AgentMiddleware[..., ...]):
    def __init__(self):
        self.tools = [my_tool_factory()]
    # ... no other hooks ...
```

You wrote a middleware whose only job is delivering a tool. The
wrap stack still calls into it every turn, even though it does
nothing. **Use a tool bundle**:

```python
def my_tool_bundle() -> list[BaseTool]:
    return [my_tool_factory()]

agent = create_agent(model=..., tools=[*my_tool_bundle()])
```

Same surface to the LLM. Zero middleware overhead. See
[tool-bundles.md](tool-bundles.md).

### "A middleware to inject a one-shot prompt"

```python
class MyPromptMiddleware(AgentMiddleware[..., ...]):
    async def awrap_model_call(self, request, call_next):
        if not request.state.get("_injected"):
            request.messages.insert(0, SystemMessage("..."))
            request.state["_injected"] = True
        return await call_next(request)
```

The "do once" pattern is a smell. Inject the prompt at agent build
time and skip the wrap:

```python
agent = create_agent(model=..., system_prompt="...")
```

Or compose the prompt yourself before calling `create_agent`.

### "A middleware to hold static config"

```python
class MyConfigMiddleware(AgentMiddleware[..., ...]):
    def __init__(self, team_config: dict): self.team_config = team_config
    # ... no wraps, just a config holder ...
```

Pass the config as a kwarg. Middleware is for runtime hooks, not
data containers. A class is fine — but it doesn't need to inherit
`AgentMiddleware`.

## Lifecycle of a middleware call

```text
create_agent(model=..., middleware=[A, B, C])
  ↓
graph.py builds the middleware list:
  [TodoList, A, B, C, Filesystem, SubAgent, Summarization,
   PatchToolCalls, PromptCaching, Memory, HITL]
  ↓
_validate_middleware_ordering(list) — fails if requires are unmet
  ↓
langchain.create_agent compiles the LangGraph
  ↓
runtime: each turn, the graph executes the middleware in order:
  - awrap_model_call wraps in reverse list-order
    (outer middleware wraps inner middleware wraps the model call)
  - before/after_tool_call hooks fire per tool dispatch
  - state mutations land in the graph's checkpointer
```

The "outer wraps inner" semantics are why list order is load-bearing.
The first item sees the request first; the last item is right next
to the model.

## Testing a middleware

```python
import pytest
from bog_agents import create_agent
from bog_agents_cli.drive.replay_model import FakeChatModel

@pytest.mark.asyncio
async def test_my_middleware_records_state():
    agent = create_agent(
        model=FakeChatModel(response_text="ok"),
        middleware=[MyMiddleware(threshold=5)],
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]})

    # Inspect the state your middleware mutated
    assert result["turn_count"] == 1
```

Use `FakeChatModel` (from the drive package) or your own
`BaseChatModel` subclass when you don't want to hit a real provider
during tests. The fake skips network and returns whatever you tell
it to.

For integration tests against the real model: mark them with
`@pytest.mark.integration` and gate on env vars in CI.

## Public API stability

Middleware that lives in `bog_agents.middleware` is treated as part
of the public SDK surface. Signatures are preserved across minor
releases; new params arrive as keyword-only with defaults; deprecated
behavior gets a one-release deprecation window before removal.

If you ship a middleware in your own package, namespace your state
keys (`my_pkg_turn_count`, not `turn_count`) so future SDK middleware
can't collide.

## Next steps

- [Tool bundles](tool-bundles.md) — when you only need tools
- [Backends](backends.md) — filesystem / shell / sandbox layer
- [Cookbook](../cookbook.md) — task-shaped recipes
- The SDK source — `libs/bog-agents/bog_agents/middleware/` has
  ~25 production middleware, all readable, all well-commented

---

*Layer carefully. Sometimes the right answer is no layer at all.*
