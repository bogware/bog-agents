# Tool Bundles

> A bundle is a function. Call it. Pass the result to
> `create_agent(tools=...)`. Done. No middleware class, no wrap-stack
> overhead, no ordering constraints to track.

## The pattern

```python
from bog_agents import create_agent
from bog_agents.tools import git_tools_bundle

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    tools=[*git_tools_bundle(working_dir=".")],
)
```

That's it. The agent now has `git_status`, `git_diff`, `git_log`,
`git_commit`, `git_add`, `git_branch`, `git_stash`, `git_blame`, and
`git_show` tools. The bundle bound them to the working directory you
passed.

No `AgentMiddleware` was constructed. No wrap stack was extended. No
`_validate_middleware_ordering` check considered them. The bundle is
a free function that returned `list[BaseTool]`.

## Why this pattern exists

The W4 audit (Wave W) noticed that ~50 of the ~80 middleware in
`bog_agents.middleware/` were doing nothing except contributing tools.
They inherited `AgentMiddleware`, set `self.tools = [...]` in
`__init__`, and never overrode any of the hook methods that make
middleware actually useful.

That's a category error. "I add tools to the agent" and "I wrap every
model call" are different concerns. Conflating them costs:

- **Cognitive overhead.** Reading a class definition to discover it
  has no hooks is a waste.
- **Implicit ordering.** Tool-only "middleware" still sits in the
  middleware list and counts against position constraints.
- **Wrap-stack frames.** Each middleware adds a frame on every model
  call, even if it does nothing.
- **Composition burden.** New users have to learn middleware to add
  tools.

The bundle pattern fixes all four.

## The exposed bundles

| Bundle | Returns | Notes |
|---|---|---|
| `git_tools_bundle(working_dir, *, auto_stage=False)` | 9 git tools | Canonical example. `working_dir` defaults to cwd. |
| `multi_edit_tool(backend, get_backend)` | 1 tool | Batch in-file edits. Already factory-shaped pre-W4 (just re-exposed). |
| `read_many_files_tool(backend, get_backend)` | 1 tool | Batched reads. Same. |

More will come as we migrate B-bucket middleware (browser_agent,
repo_map, plugin_system, the various MCP-style helpers). The pattern
is land-pattern-first, migrate-incrementally.

## Writing your own bundle

A bundle is the simplest possible thing:

```python
# my_pkg/bundles.py

from pathlib import Path
from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool


def vegetable_tools_bundle(
    db_path: Path,
    *,
    include_destructive: bool = False,
) -> list[BaseTool]:
    """Return tools that talk to a vegetable database bound to ``db_path``."""

    def add_vegetable(
        _runtime: ToolRuntime[None, Any],
        name: Annotated[str, "Vegetable name"],
        color: Annotated[str, "Hex color"] = "#3a5",
    ) -> str:
        # ... write to db_path ...
        return f"Added {name}."

    def list_vegetables(_runtime: ToolRuntime[None, Any]) -> str:
        # ... read from db_path ...
        return "carrot, beet, parsnip"

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            name="add_vegetable",
            description="Add a vegetable to the database.",
            func=add_vegetable,
        ),
        StructuredTool.from_function(
            name="list_vegetables",
            description="List every vegetable.",
            func=list_vegetables,
        ),
    ]

    if include_destructive:
        def delete_vegetable(
            _runtime: ToolRuntime[None, Any],
            name: Annotated[str, "Vegetable to delete"],
        ) -> str:
            # ... delete from db_path ...
            return f"Deleted {name}."

        tools.append(StructuredTool.from_function(
            name="delete_vegetable",
            description="Delete a vegetable. Irreversible.",
            func=delete_vegetable,
        ))

    return tools
```

```python
from my_pkg.bundles import vegetable_tools_bundle
from bog_agents import create_agent

agent = create_agent(
    model="anthropic:claude-opus-4-7",
    tools=[*vegetable_tools_bundle(db_path="/data/veg.db", include_destructive=False)],
)
```

That's it. No class, no `__init__`, no hooks, no state schema.

## Rules of thumb

- **One bundle = one logical group of tools.** Git tools live in
  one bundle. Database read tools and database write tools can live
  in separate bundles so callers can pick.
- **Bundles bind configuration at construction.** `working_dir`,
  `db_path`, an HTTP client, etc. — capture them in the closure.
  Tools become arity-correct ToolRuntime functions.
- **Bundles are pure.** Two calls with the same arguments return
  independent tool lists with equivalent behavior. No shared
  mutable state between invocations.
- **Use `ToolRuntime[None, Any]` for the first parameter.** That's
  the LangChain convention. Pyright will accept it. The agent
  injects the runtime automatically.

## When to keep a middleware class instead

The bundle is the lean form. Stick with a middleware class when:

- You need hooks beyond `tools` (`awrap_model_call`,
  `before_tool_call`, etc.).
- You need state that persists across turns AND survives
  checkpointing.
- You need the agent to inject system-prompt context that depends
  on per-call request state.

If you only need tools and a long-lived config object, **the config
object can be a normal class** — pass it as a closed-over value into
the bundle. You don't need to inherit `AgentMiddleware` to hold
config.

## Migration path for existing middleware

Today the `GitToolsMiddleware` class is a thin shim that delegates
to `git_tools_bundle`. Same outcome two ways:

```python
# Old (still works, kept for back-compat)
from bog_agents.middleware import GitToolsMiddleware
agent = create_agent(model=..., middleware=[GitToolsMiddleware(working_dir=".")])

# New (prefer this for new code)
from bog_agents.tools import git_tools_bundle
agent = create_agent(model=..., tools=[*git_tools_bundle(working_dir=".")])
```

The shim approach lets us move B-bucket middleware to bundles
incrementally without breaking existing imports.

## Performance

A bundle adds zero wrap-stack frames. The tool dispatch itself is
the same code path as any other tool. There's no overhead for the
bundle pattern — it's *strictly* faster than the middleware equivalent
because every middleware adds a frame to every model call, while a
bundle is just tools sitting in the registry.

For the canonical example: the SDK's model-call wrap stack is
8-deep with the default middleware set. Replacing all B-bucket
middleware with bundles would take it back to 4-deep. The user-
facing latency improvement is small per call but compounds over
long sessions.

## Next steps

- [Middleware](middleware.md) — when you actually need one
- [Quickstart](quickstart.md) — `create_agent` happy path
- The `bog_agents.tools.bundles` source — readable, comment-rich

---

*Less ceremony. Same outcome.*
