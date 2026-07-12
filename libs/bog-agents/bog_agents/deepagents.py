"""deepagents compatibility surface for bog-agents.

bog-agents and `deepagents` (langchain-ai/deepagents) share a common root, and
this module lets code written against the deepagents public API run unchanged on
bog-agents — and vice versa. Import the deepagents-style names from here (or from
the top-level `bog_agents` package, which re-exports the same symbols):

```python
from bog_agents.deepagents import create_deep_agent, DeepAgentState, FilesystemPermission

agent = create_deep_agent(model="anthropic:claude-sonnet-4-6")
```

`create_deep_agent` is a thin wrapper over `bog_agents.create_agent` that mirrors
the deepagents signature exactly and, like deepagents, defaults `state_schema` to
`DeepAgentState` so checkpoint growth stays O(N) via the `DeltaChannel` messages
reducer. Everything else is a direct re-export of the corresponding bog-agents
symbol, so a `SubAgent`/`HarnessProfile`/`FilesystemPermission` constructed here
is the same object bog-agents uses internally.

Parity note: deepagents does **not** ship an `async_create_deep_agent`, so this
module intentionally omits one. Async execution is available through the compiled
graph's `ainvoke`/`astream` and the `AsyncSubAgent` mechanism, exactly as in
deepagents.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from bog_agents._version import __version__
from bog_agents.graph import DeepAgentState, SystemPromptConfig, create_agent
from bog_agents.middleware.async_subagents import AsyncSubAgent, AsyncSubAgentMiddleware
from bog_agents.middleware.filesystem import FilesystemMiddleware, FsToolName
from bog_agents.middleware.memory import MemoryMiddleware
from bog_agents.middleware.permissions import FilesystemPermission
from bog_agents.middleware.rubric import RubricMiddleware
from bog_agents.middleware.subagents import (
    SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY,
    CompiledSubAgent,
    SubAgent,
    SubAgentMiddleware,
    create_sub_agent,
)
from bog_agents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    HarnessProfileConfig,
    register_harness_profile,
)
from bog_agents.profiles.provider.provider_profiles import ProviderProfile, register_provider_profile

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from langchain.agents.middleware import InterruptOnConfig
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain.agents.structured_output import ResponseFormat
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import SystemMessage
    from langchain_core.tools import BaseTool
    from langgraph.cache.base import BaseCache
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.store.base import BaseStore
    from langgraph.types import Checkpointer

    from bog_agents.backends.protocol import BackendFactory, BackendProtocol

__all__ = [
    "SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY",
    "AsyncSubAgent",
    "AsyncSubAgentMiddleware",
    "CompiledSubAgent",
    "DeepAgentState",
    "FilesystemMiddleware",
    "FilesystemPermission",
    "FsToolName",
    "GeneralPurposeSubagentProfile",
    "HarnessProfile",
    "HarnessProfileConfig",
    "MemoryMiddleware",
    "ProviderProfile",
    "RubricMiddleware",
    "SubAgent",
    "SubAgentMiddleware",
    "SystemPromptConfig",
    "__version__",
    "create_deep_agent",
    "create_sub_agent",
    "register_harness_profile",
    "register_provider_profile",
]


def create_deep_agent(
    model: str | BaseChatModel | None = None,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    *,
    system_prompt: str | SystemMessage | SystemPromptConfig | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagents: Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent] | None = None,
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    backend: BackendProtocol | BackendFactory | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    response_format: ResponseFormat | type[Any] | dict[str, Any] | None = None,
    state_schema: type[Any] | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: Checkpointer | None = None,
    store: BaseStore | None = None,
    debug: bool = False,
    name: str | None = None,
    cache: BaseCache | None = None,
    max_turns: int = 9_999,
) -> CompiledStateGraph:
    """Create a deep agent (deepagents-compatible entry point).

    A drop-in for `deepagents.create_deep_agent` that delegates to
    `bog_agents.create_agent`. The signature matches deepagents exactly. Like
    deepagents, `state_schema` defaults to `DeepAgentState` so the `DeltaChannel`
    messages reducer is active and checkpoint growth stays linear; pass an
    explicit `state_schema` (a `DeepAgentState` subclass) to extend it, or the
    bog-agents default `AgentState` to opt out.

    See `bog_agents.create_agent` for full parameter documentation. The
    bog-agents-only `config`/`features` feature-flag system is intentionally not
    exposed here to keep this surface identical to deepagents; use
    `create_agent` directly when you need it.

    Args:
        model: Model spec string (`provider:model`) or a `BaseChatModel`.
        tools: Additional tools, merged with the built-in suite.
        system_prompt: Custom instructions prepended to the base prompt.
        middleware: Extra middleware appended to the stack.
        subagents: `SubAgent` / `CompiledSubAgent` / `AsyncSubAgent` specs.
        skills: Skill source paths.
        memory: `AGENTS.md` memory file paths.
        permissions: `FilesystemPermission` rules for filesystem tools.
        backend: Storage/execution backend.
        interrupt_on: Per-tool human-in-the-loop configuration.
        response_format: Structured-output response format.
        state_schema: Custom state schema; defaults to `DeepAgentState`.
        context_schema: Run-scoped context schema.
        checkpointer: Checkpointer for cross-run persistence.
        store: Persistent store (required by `StoreBackend`).
        debug: Enable debug mode.
        name: Agent name.
        cache: Node cache.
        max_turns: Maximum model turns before the run stops. Defaults to
            `9_999` to mirror the deepagents recursion budget.

    Returns:
        A compiled deep agent graph.
    """
    return create_agent(
        model,
        tools,
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=list(subagents) if subagents is not None else None,
        skills=skills,
        memory=memory,
        permissions=permissions,
        backend=backend,
        interrupt_on=interrupt_on,
        response_format=cast("ResponseFormat | None", response_format),
        state_schema=state_schema if state_schema is not None else DeepAgentState,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name=name,
        cache=cache,
        max_turns=max_turns,
    )
