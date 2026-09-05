"""Middleware for providing subagents to an agent via a `task` tool."""

import contextlib
import warnings
from collections.abc import Awaitable, Callable, Generator, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal, NotRequired, TypedDict, Unpack, cast

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig
from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ModelRequest, ModelResponse, ResponseT
from langchain.agents.structured_output import ResponseFormat
from langchain.tools import BaseTool, ToolRuntime
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from langsmith.run_helpers import get_tracing_context, tracing_context

from bog_agents.backends.protocol import BackendFactory, BackendProtocol
from bog_agents.middleware._private_state import private_state_field_names
from bog_agents.middleware._utils import append_to_system_message

if TYPE_CHECKING:
    from bog_agents.cost_ledger import CostLedger
from bog_agents.middleware.permissions import FilesystemPermission

__all__ = [
    "FORK_SUBAGENT_DESCRIPTION",
    "SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY",
    "CompiledSubAgent",
    "SubAgent",
    "SubAgentMiddleware",
    "create_sub_agent",
    "seed_fork_messages",
    "subagent_private_state_keys",
]

SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY = "__deepagents_subagent_response_format"
"""Configurable key used by `task`-tool callers to request a dynamic response format.

The literal is deliberately identical to upstream deepagents': it is a wire key
that third-party callers (and upstream-typed code) put into
`config["configurable"]`, so renaming it would silently break interop.
"""


class SubAgent(TypedDict):
    """Specification for an agent.

    When using `create_agent`, subagents automatically receive a default middleware
    stack (TodoListMiddleware, FilesystemMiddleware, SummarizationMiddleware, etc.) before
    any custom `middleware` specified in this spec.

    Required fields:
        name: Unique identifier for the subagent.

            The main agent uses this name when calling the `task()` tool.
        description: What this subagent does.

            Be specific and action-oriented. The main agent uses this to decide when to delegate.
        system_prompt: Instructions for the subagent.

            Include tool usage guidance and output format requirements.

    Optional fields:
        tools: Tools the subagent can use.

            If not specified, inherits tools from the main agent via `default_tools`.
        model: Override the main agent's model.

            Use the format `'provider:model-name'` (e.g., `'openai:gpt-4o'`).
        middleware: Additional middleware for custom behavior, logging, or rate limiting.
        interrupt_on: Configure human-in-the-loop for specific tools.

            Requires a checkpointer.
        skills: Skill source paths for SkillsMiddleware.

            List of paths to skill directories (e.g., `["/skills/user/", "/skills/project/"]`).
    """

    name: str
    """Unique identifier for the subagent."""

    description: str
    """What this subagent does. The main agent uses this to decide when to delegate."""

    system_prompt: str
    """Instructions for the subagent."""

    tools: NotRequired[Sequence[BaseTool | Callable | dict[str, Any]]]
    """Tools the subagent can use. If not specified, inherits from main agent."""

    model: NotRequired[str | BaseChatModel]
    """Override the main agent's model. Use `'provider:model-name'` format."""

    middleware: NotRequired[list[AgentMiddleware]]
    """Additional middleware for custom behavior."""

    interrupt_on: NotRequired[dict[str, bool | InterruptOnConfig]]
    """Configure human-in-the-loop for specific tools."""

    skills: NotRequired[list[str]]
    """Skill source paths for SkillsMiddleware."""
    mode: NotRequired[Literal["isolated", "fork"]]
    """`isolated` (default) starts the child with only the task; `fork` (ROADMAP #71)
    seeds it with the parent's conversation so far — same tools, same base prompt —
    so its first model call rides the parent's prefix."""

    permissions: NotRequired[list[FilesystemPermission]]
    """Filesystem permission rules for this subagent.

    When set, replaces the parent agent's `permissions` entirely (rules are
    not merged). When omitted, the subagent inherits the parent's rules. See
    `create_agent`'s `permissions` parameter for rule semantics."""

    response_format: NotRequired[ResponseFormat[Any] | type | dict[str, Any]]
    """Structured-output response format for this subagent.

    When set, the subagent is compiled with `response_format`, so it produces a
    `structured_response` conforming to the given schema.

    Accepted forms (from `langchain.agents.structured_output`):

    - `ToolStrategy(schema)` — use tool calling to extract structured output.
    - `ProviderStrategy(schema)` — use the provider's native structured-output mode.
    - `AutoStrategy(schema)` — pick the best strategy automatically.
    - A bare Python `type` (Pydantic `BaseModel` subclass, dataclass, or
      `TypedDict`), equivalent to `AutoStrategy(schema)`.
    - A JSON-schema `dict`.
    """


class CompiledSubAgent(TypedDict):
    """A pre-compiled agent spec.

    !!! note

        The runnable's state schema must include a 'messages' key.

        This is required for the subagent to communicate results back to the main agent.

    When the subagent completes, the final message in the 'messages' list will be
    extracted and returned as a `ToolMessage` to the parent agent.
    """

    name: str
    """Unique identifier for the subagent."""

    description: str
    """What this subagent does."""

    runnable: Runnable
    """A custom agent implementation.

    Create a custom agent using either:

    1. LangChain's [`create_agent()`](https://docs.langchain.com/oss/python/langchain/quickstart)
    2. A custom graph using [`langgraph`](https://docs.langchain.com/oss/python/langgraph/quickstart)

    If you're creating a custom graph, make sure the state schema includes a 'messages' key.
    This is required for the subagent to communicate results back to the main agent.
    """


DEFAULT_SUBAGENT_PROMPT = (
    "In order to complete the objective that the user asks of you, you have access to a number of standard tools.\n\n"
    "CRITICAL — no fabricated tool output:\n"
    "- Never claim that a shell command, test, build, install, or other tool "
    "invocation 'ran' or 'passed' unless you actually called the tool in this "
    "turn and observed its output. Do not paraphrase what you think the "
    "output would have been.\n"
    "- If a step you were asked to perform requires a tool you cannot call "
    "(missing permission, missing dependency, no shell access), state that "
    "explicitly in your final report — e.g. 'I could not run npm test "
    "because <reason>.' Do not silently substitute prose for execution.\n"
    "- Your final report to the parent agent must distinguish 'I ran X and "
    "saw Y' from 'I read the code and reasoned that X would produce Y'.\n\n"
    "The calling agent only sees your final assistant message, not your "
    "intermediate work, tool results, or status tracking. Ensure your final "
    "response contains the complete answer."
)

# State keys that are always excluded at both subagent state-crossing points:
# parent -> child (building the child's input state) and child -> parent
# (folding the child's result back into the parent's state).
#
# 1. `messages` is handled explicitly so only the subagent's final message is
#    forwarded to the parent (and only the task description is forwarded to the
#    child).
# 2. `todos` and `structured_response` have no reducer and no coherent meaning
#    when carried across the subagent boundary.
# 3. `skills_metadata` and `memory_contents` are middleware bookkeeping. They are
#    also `PrivateStateAttr`-marked on their schemas — and any such field is now
#    filtered generically via `private_state_keys` (see
#    `SubAgentMiddleware.private_state_keys`) — but they stay listed here so the
#    filter holds even when a caller constructs the middleware without wiring
#    `private_state_keys` from the assembled middleware stack.
_EXCLUDED_STATE_KEYS = {"messages", "todos", "structured_response", "skills_metadata", "memory_contents"}

TASK_TOOL_DESCRIPTION = """Launch an ephemeral subagent to handle complex, multi-step independent tasks with isolated context windows.

Available agent types and the tools they have access to:
{available_agents}

When using the Task tool, you must specify a subagent_type parameter to select which agent type to use.

## Usage notes:
1. Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses
2. When the agent is done, it will return a single message back to you. The result returned by the agent is not visible to the user. To show the user the result, you should send a text message back to the user with a concise summary of the result.
3. Each agent invocation is stateless. You will not be able to send additional messages to the agent, nor will the agent be able to communicate with you outside of its final report. Therefore, your prompt should contain a highly detailed task description for the agent to perform autonomously and you should specify exactly what information the agent should return back to you in its final and only message to you.
4. The agent's outputs should generally be trusted
5. Clearly tell the agent whether you expect it to create content, perform analysis, or just do research (search, file reads, web fetches, etc.), since it is not aware of the user's intent
6. If the agent description mentions that it should be used proactively, then you should try your best to use it without the user having to ask for it first. Use your judgement.
7. When only the general-purpose agent is provided, you should use it for all tasks. It is great for isolating context and token usage, and completing specific, complex tasks, as it has all the same capabilities as the main agent.

### Example usage of the general-purpose agent:

<example_agent_descriptions>
"general-purpose": use this agent for general purpose tasks, it has access to all tools as the main agent.
</example_agent_descriptions>

<example>
User: "I want to conduct research on the accomplishments of Lebron James, Michael Jordan, and Kobe Bryant, and then compare them."
Assistant: *Uses the task tool in parallel to conduct isolated research on each of the three players*
Assistant: *Synthesizes the results of the three isolated research tasks and responds to the User*
<commentary>
Research is a complex, multi-step task in it of itself.
The research of each individual player is not dependent on the research of the other players.
The assistant uses the task tool to break down the complex objective into three isolated tasks.
Each research task only needs to worry about context and tokens about one player, then returns synthesized information about each player as the Tool Result.
This means each research task can dive deep and spend tokens and context deeply researching each player, but the final result is synthesized information, and saves us tokens in the long run when comparing the players to each other.
</commentary>
</example>

<example>
User: "Analyze a single large code repository for security vulnerabilities and generate a report."
Assistant: *Launches a single `task` subagent for the repository analysis*
Assistant: *Receives report and integrates results into final summary*
<commentary>
Subagent is used to isolate a large, context-heavy task, even though there is only one. This prevents the main thread from being overloaded with details.
If the user then asks followup questions, we have a concise report to reference instead of the entire history of analysis and tool calls, which is good and saves us time and money.
</commentary>
</example>

<example>
User: "Schedule two meetings for me and prepare agendas for each."
Assistant: *Calls the task tool in parallel to launch two `task` subagents (one per meeting) to prepare agendas*
Assistant: *Returns final schedules and agendas*
<commentary>
Tasks are simple individually, but subagents help silo agenda preparation.
Each subagent only needs to worry about the agenda for one meeting.
</commentary>
</example>

<example>
User: "I want to order a pizza from Dominos, order a burger from McDonald's, and order a salad from Subway."
Assistant: *Calls tools directly in parallel to order a pizza from Dominos, a burger from McDonald's, and a salad from Subway*
<commentary>
The assistant did not use the task tool because the objective is super simple and clear and only requires a few trivial tool calls.
It is better to just complete the task directly and NOT use the `task` tool.
</commentary>
</example>

### Example usage with custom agents:

<example_agent_descriptions>
"content-reviewer": use this agent after you are done creating significant content or documents
"greeting-responder": use this agent when to respond to user greetings with a friendly joke
"research-analyst": use this agent to conduct thorough research on complex topics
</example_agent_descriptions>

<example>
user: "Please write a function that checks if a number is prime"
assistant: Sure let me write a function that checks if a number is prime
assistant: First let me use the Write tool to write a function that checks if a number is prime
assistant: I'm going to use the Write tool to write the following code:
<code>
function isPrime(n) {{
  if (n <= 1) return false
  for (let i = 2; i * i <= n; i++) {{
    if (n % i === 0) return false
  }}
  return true
}}
</code>
<commentary>
Since significant content was created and the task was completed, now use the content-reviewer agent to review the work
</commentary>
assistant: Now let me use the content-reviewer agent to review the code
assistant: Uses the Task tool to launch with the content-reviewer agent
</example>

<example>
user: "Can you help me research the environmental impact of different renewable energy sources and create a comprehensive report?"
<commentary>
This is a complex research task that would benefit from using the research-analyst agent to conduct thorough analysis
</commentary>
assistant: I'll help you research the environmental impact of renewable energy sources. Let me use the research-analyst agent to conduct comprehensive research on this topic.
assistant: Uses the Task tool to launch with the research-analyst agent, providing detailed instructions about what research to conduct and what format the report should take
</example>

<example>
user: "Hello"
<commentary>
Since the user is greeting, use the greeting-responder agent to respond with a friendly joke
</commentary>
assistant: "I'm going to use the Task tool to launch with the greeting-responder agent"
</example>"""

TASK_SYSTEM_PROMPT = """## `task` (subagent spawner)

You have access to a `task` tool to launch short-lived subagents that handle isolated tasks. These agents are ephemeral — they live only for the duration of the task and return a single result.

When to use the task tool:

- When a task is complex and multi-step, and can be fully delegated in isolation
- When a task is independent of other tasks and can run in parallel
- When a task requires focused reasoning or heavy token/context usage that would bloat the orchestrator thread
- When sandboxing improves reliability (e.g. code execution, structured searches, data formatting)
- When you only care about the output of the subagent, and not the intermediate steps (ex. performing a lot of research and then returned a synthesized report, performing a series of computations or lookups to achieve a concise, relevant answer.)

Subagent lifecycle:

1. **Spawn** → Provide clear role, instructions, and expected output
2. **Run** → The subagent completes the task autonomously
3. **Return** → The subagent provides a single structured result
4. **Reconcile** → Incorporate or synthesize the result into the main thread

When NOT to use the task tool:

- If you need to see the intermediate reasoning or steps after the subagent has completed (the task tool hides them)
- If the task is trivial (a few tool calls or simple lookup)
- If delegating does not reduce token usage, complexity, or context switching
- If splitting would add latency without benefit

## Important Task Tool Usage Notes to Remember

- Whenever possible, parallelize the work that you do. This is true for both tool_calls, and for tasks. Whenever you have independent steps to complete - make tool_calls, or kick off tasks (subagents) in parallel to accomplish them faster. This saves time for the user, which is incredibly important.
- Remember to use the `task` tool to silo independent tasks within a multi-part objective.
- You should use the `task` tool whenever you have a complex task that will take multiple steps, and is independent from other tasks that the agent needs to complete. These agents are highly competent and efficient."""


DEFAULT_GENERAL_PURPOSE_DESCRIPTION = "General-purpose agent for researching complex questions, searching for files and content, and executing multi-step tasks. When you are searching for a keyword or file and are not confident that you will find the right match in the first few tries use this agent to perform the search for you. This agent has access to all tools as the main agent."

# Base spec for general-purpose subagent (caller adds model, tools, middleware)
GENERAL_PURPOSE_SUBAGENT: SubAgent = {
    "name": "general-purpose",
    "description": DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
    "system_prompt": DEFAULT_SUBAGENT_PROMPT,
}

FORK_SUBAGENT_DESCRIPTION = (
    "A fork of this agent: the same instructions, tools and the conversation so far. "
    "Use it for a side task that needs everything you already know (a second opinion, "
    "a parallel investigation, a follow-up that must not lose context) rather than a fresh start."
)
"""Description of the built-in `fork` subagent (ROADMAP #71)."""


class _SubagentSpec(TypedDict):
    """Internal spec for building the task tool."""

    name: str
    description: str
    runnable: Runnable
    mode: NotRequired[str]
    raw: NotRequired[SubAgent]
    """The fully-resolved raw spec this runnable was compiled from.

    Absent for `CompiledSubAgent` entries — the caller owns those runnables and
    they cannot be recompiled. Present for raw specs, so the `task` tool can
    recompile the subagent when a caller requests a per-call `response_format`
    via `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY`.
    """


_BASE_AGENT_STATE_PRIVATE_KEYS = private_state_field_names(AgentState)
"""Private keys on langchain's own `AgentState` (currently `jump_to`).

`private_state_field_names` reports these for *every* schema, because every
middleware state schema inherits from `AgentState`. They are langchain's
control-flow bookkeeping rather than middleware bookkeeping, so
`subagent_private_state_keys` subtracts them back out.
"""


def subagent_private_state_keys(*schemas: type) -> frozenset[str]:
    """Collect the `PrivateStateAttr` field names that middleware state schemas added.

    Thin wrapper over `private_state_field_names` that drops the keys langchain's
    base `AgentState` marks private, leaving only fields contributed by middleware.
    Those are exactly the keys that must not cross the subagent boundary — pass the
    result to `SubAgentMiddleware(private_state_keys=...)` (or assign it to the
    `private_state_keys` property once the middleware stack is assembled).

    Args:
        schemas: State schemas to inspect — typically the agent's `state_schema`
            plus every `state_schema` declared by the assembled middleware stack.

    Returns:
        Private field names contributed by the given schemas.
    """
    return private_state_field_names(*schemas) - _BASE_AGENT_STATE_PRIVATE_KEYS


@contextlib.contextmanager
def _subagent_tracing_context() -> Generator[None, None, None]:
    """Tag runs created inside this block with `ls_agent_type="subagent"`.

    Sets `ls_agent_type` on the LangSmith tracing context's `metadata`, which is
    propagated to LangSmith runs. This mirrors langchain's `ls_agent_type="root"`
    tagging for the top-level agent.

    Every other field of the current tracing context (parent run, client, tags,
    project name, ...) is splatted through unchanged, so an enclosing context is
    never clobbered — `metadata` is the only field this touches, and only
    additively.

    Yields:
        `None`, with the merged tracing context active.
    """
    current = get_tracing_context()
    merged_metadata = {**(current.get("metadata") or {}), "ls_agent_type": "subagent"}
    kwargs: dict[str, Any] = {**current, "metadata": merged_metadata}
    with tracing_context(**kwargs):
        yield


def create_sub_agent(
    spec: SubAgent,
    *,
    state_schema: type | None = None,
    response_format: ResponseFormat[Any] | type | dict[str, Any] | None = None,
) -> Runnable:
    """Compile a runnable agent from a raw `SubAgent` spec.

    This is the single `create_agent` entrypoint for raw subagent specs, so every
    construction site honors the spec's `model`, `tools`, `middleware`,
    `interrupt_on`, and `response_format`. `CompiledSubAgent` runnables are built
    by the caller and are not routed through here.

    Args:
        spec: Subagent spec to compile. Must specify `model` and `tools`.
        state_schema: Base graph state schema forwarded to `create_agent`. When
            `None`, `create_agent`'s default state schema is used.
        response_format: Response format override for this compiled instance. When
            `None`, the spec's own `response_format` (if any) is used.

    Returns:
        A compiled agent ready for `task`-tool invocation.

    Raises:
        ValueError: If `spec` is missing `model` or `tools`.
    """
    if "model" not in spec:
        msg = f"SubAgent '{spec['name']}' must specify 'model'"
        raise ValueError(msg)
    if "tools" not in spec:
        msg = f"SubAgent '{spec['name']}' must specify 'tools'"
        raise ValueError(msg)

    # Deferred import: keeps `import bog_agents.middleware` cheap and avoids an
    # import cycle through the profiles package that `_models` pulls in.
    from bog_agents._models import resolve_model

    model = resolve_model(spec["model"])
    middleware: list[AgentMiddleware] = list(spec.get("middleware", []))

    interrupt_on = spec.get("interrupt_on")
    if interrupt_on:
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on))

    selected_response_format = response_format if response_format is not None else spec.get("response_format")
    create_agent_kwargs: dict[str, Any] = {
        "system_prompt": spec["system_prompt"],
        "tools": spec["tools"],
        "middleware": middleware,
        "name": spec["name"],
        "response_format": selected_response_format,
    }
    if state_schema is not None:
        create_agent_kwargs["state_schema"] = state_schema

    return create_agent(model, **create_agent_kwargs)


def _get_subagent_response_format(
    runtime: ToolRuntime,
) -> ResponseFormat[Any] | type | dict[str, Any] | None:
    """Return the response format carried in this `task` tool call's config.

    Args:
        runtime: Tool runtime for the current `task` call.

    Returns:
        The response format stored under `SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY`, or
        `None` when the caller did not request one.
    """
    config = runtime.config
    configurable = config.get("configurable") if isinstance(config, dict) else None
    if not isinstance(configurable, dict):
        return None
    return configurable.get(SUBAGENT_RESPONSE_FORMAT_CONFIG_KEY)


def _get_subagents_legacy(
    *,
    default_model: str | BaseChatModel,
    default_tools: Sequence[BaseTool | Callable | dict[str, Any]],
    default_middleware: list[AgentMiddleware] | None,
    default_interrupt_on: dict[str, bool | InterruptOnConfig] | None,
    subagents: list[SubAgent | CompiledSubAgent],
    general_purpose_agent: bool,
    state_schema: type | None = None,
) -> list[_SubagentSpec]:
    """Create subagent instances from specifications.

    Args:
        default_model: Default model for subagents that don't specify one.
        default_tools: Default tools for subagents that don't specify tools.
        default_middleware: Middleware to apply to all subagents. If `None`,
            no default middleware is applied.
        default_interrupt_on: The tool configs to use for the default general-purpose subagent. These
            are also the fallback for any subagents that don't specify their own tool configs.
        subagents: List of agent specifications or pre-compiled agents.
        general_purpose_agent: Whether to include a general-purpose subagent.
        state_schema: Base graph state schema forwarded to raw subagent specs.

    Returns:
        List of subagent specs containing name, description, and runnable.
    """
    # Use empty list if None (no default middleware)
    default_subagent_middleware = default_middleware or []

    specs: list[_SubagentSpec] = []

    # Create general-purpose agent if enabled
    if general_purpose_agent:
        general_purpose_spec: SubAgent = {
            "name": "general-purpose",
            "description": DEFAULT_GENERAL_PURPOSE_DESCRIPTION,
            "system_prompt": DEFAULT_SUBAGENT_PROMPT,
            "model": default_model,
            "tools": list(default_tools),
            "middleware": [*default_subagent_middleware],
        }
        if default_interrupt_on:
            general_purpose_spec["interrupt_on"] = default_interrupt_on
        specs.append(
            {
                "name": general_purpose_spec["name"],
                "description": general_purpose_spec["description"],
                "runnable": create_sub_agent(general_purpose_spec, state_schema=state_schema),
                "raw": general_purpose_spec,
            }
        )

    # Process custom subagents
    for agent_ in subagents:
        if "runnable" in agent_:
            custom_agent = cast("CompiledSubAgent", agent_)
            specs.append(
                {
                    "name": custom_agent["name"],
                    "description": custom_agent["description"],
                    "runnable": custom_agent["runnable"],
                }
            )
            continue

        # Resolve this spec's defaults, then compile through the shared factory.
        # Routing raw specs through `create_sub_agent` (instead of calling
        # `create_agent` inline) is what makes the spec's `response_format`
        # actually reach the subagent rather than being silently dropped.
        resolved: SubAgent = {
            "name": agent_["name"],
            "description": agent_["description"],
            "system_prompt": agent_["system_prompt"],
            "model": agent_.get("model", default_model),
            "tools": agent_.get("tools", list(default_tools)),
            "middleware": [*default_subagent_middleware, *agent_["middleware"]] if "middleware" in agent_ else [*default_subagent_middleware],
        }
        interrupt_on = agent_.get("interrupt_on", default_interrupt_on)
        if interrupt_on:
            resolved["interrupt_on"] = interrupt_on
        if "response_format" in agent_:
            resolved["response_format"] = agent_["response_format"]
        if "mode" in agent_:
            resolved["mode"] = agent_["mode"]

        specs.append(
            {
                "name": resolved["name"],
                "description": resolved["description"],
                "runnable": create_sub_agent(resolved, state_schema=state_schema),
                "raw": resolved,
            }
        )

    return specs


def _cap_refusal(cost_ledger: "CostLedger | None", subagent_type: str) -> str | None:
    """Return a refusal message when the session ledger forbids another spawn.

    v6 SDK-7: `CostLedger`/`RunawayCaps` used to be consulted only by
    `teams.run_team`, so `max_subagents` and `max_cost_usd` never fired on the
    default `task` fan-out path. The cost cap is checked first (no counter),
    then the spawn is counted against `max_subagents`.

    Args:
        cost_ledger: The session ledger, or `None` when uncapped.
        subagent_type: The subagent the model asked for (for the message).

    Returns:
        The tool result to return instead of spawning, or `None` to proceed.
    """
    if cost_ledger is None:
        return None
    cost = cost_ledger.check_cost()
    if not cost.allowed:
        return f"Cannot spawn subagent `{subagent_type}`: {cost.reason}. Finish with the results you already have."
    spawn = cost_ledger.register_subagent_spawn()
    if not spawn.allowed:
        return f"Cannot spawn subagent `{subagent_type}`: {spawn.reason}. Finish with the results you already have."
    return None


def seed_fork_messages(parent_messages: Sequence[BaseMessage], description: str) -> list[BaseMessage]:
    """The messages a fork-mode child starts from (ROADMAP #71).

    The parent's conversation minus its system messages (the child has its own)
    and minus the trailing `AIMessage` whose `task` tool call is the one being
    served — it has no `ToolMessage` yet and would leave the child's history
    unbalanced — followed by the task as a `HumanMessage`.

    Args:
        parent_messages: The parent graph's canonical message list.
        description: The task for the child.

    Returns:
        A well-formed message list ending with the task.
    """
    seed: list[BaseMessage] = [m for m in parent_messages if not isinstance(m, SystemMessage)]
    if seed and isinstance(seed[-1], AIMessage) and seed[-1].tool_calls:
        seed.pop()
    return [*seed, HumanMessage(content=description)]


def _build_task_tool(
    subagents: list[_SubagentSpec],
    task_description: str | None = None,
    *,
    private_state_keys: frozenset[str] = frozenset(),
    state_schema: type | None = None,
    cost_ledger: "CostLedger | None" = None,
) -> BaseTool:
    """Create a task tool from pre-built subagent graphs.

    This is the shared implementation used by both the legacy API and new API.

    Args:
        subagents: List of subagent specs containing name, description, and runnable.
        task_description: Custom description for the task tool. If `None`,
            uses default template. Supports `{available_agents}` placeholder.
        private_state_keys: `PrivateStateAttr`-marked state keys that must not cross
            the subagent boundary in either direction.
        state_schema: Base graph state schema used when a raw spec is recompiled to
            satisfy a per-call `response_format`.
        cost_ledger: Session ledger whose `RunawayCaps` gate every spawn
            (v6 SDK-7). `None` leaves spawns uncounted and uncapped.

    Returns:
        A StructuredTool that can invoke subagents by type.
    """
    # Build the graphs dict and descriptions from the unified spec list
    subagent_graphs: dict[str, Runnable] = {spec["name"]: spec["runnable"] for spec in subagents}
    raw_specs: dict[str, SubAgent] = {spec["name"]: spec["raw"] for spec in subagents if "raw" in spec}
    fork_modes = {name: str(raw.get("mode", "isolated")) == "fork" for name, raw in raw_specs.items()}
    subagent_description_str = "\n".join(f"- {s['name']}: {s['description']}" for s in subagents)

    # Use custom description if provided, otherwise use default template
    if task_description is None:
        description = TASK_TOOL_DESCRIPTION.format(available_agents=subagent_description_str)
    elif "{available_agents}" in task_description:
        description = task_description.format(available_agents=subagent_description_str)
    else:
        description = task_description

    def _return_command_with_state_update(result: dict, tool_call_id: str) -> Command:
        # Validate that the result contains a 'messages' key
        if "messages" not in result:
            error_msg = (
                "CompiledSubAgent must return a state containing a 'messages' key. "
                "Custom StateGraphs used with CompiledSubAgent should include 'messages' "
                "in their state schema to communicate results back to the main agent."
            )
            raise ValueError(error_msg)

        # child -> parent crossing: withhold the always-excluded keys *and* every
        # private middleware field, so subagent bookkeeping can't leak upward.
        state_update = {k: v for k, v in result.items() if k not in _EXCLUDED_STATE_KEYS and k not in private_state_keys}
        # Strip trailing whitespace to prevent API errors with Anthropic
        message_text = result["messages"][-1].text.rstrip() if result["messages"][-1].text else ""
        return Command(
            update={
                **state_update,
                "messages": [ToolMessage(message_text, tool_call_id=tool_call_id)],
            }
        )

    def _select_subagent(subagent_type: str, runtime: ToolRuntime) -> Runnable:
        """Return the runnable for this invocation, honoring a per-call response format."""
        response_format = _get_subagent_response_format(runtime)
        if response_format is None:
            return subagent_graphs[subagent_type]
        if subagent_type not in raw_specs:
            msg = f'response_format cannot be used with compiled subagent "{subagent_type}"; dynamic schemas require a raw SubAgent spec.'
            raise ValueError(msg)
        return create_sub_agent(raw_specs[subagent_type], state_schema=state_schema, response_format=response_format)

    def _validate_and_prepare_state(subagent_type: str, description: str, runtime: ToolRuntime) -> tuple[Runnable, dict]:
        """Prepare state for invocation."""
        subagent = _select_subagent(subagent_type, runtime)
        # parent -> child crossing: build a new state dict (never mutate the
        # parent's) with the always-excluded and private keys withheld.
        subagent_state = {k: v for k, v in runtime.state.items() if k not in _EXCLUDED_STATE_KEYS and k not in private_state_keys}
        if fork_modes.get(subagent_type):
            subagent_state["messages"] = seed_fork_messages(runtime.state.get("messages", []), description)
        else:
            subagent_state["messages"] = [HumanMessage(content=description)]
        return subagent, subagent_state

    def task(
        description: Annotated[
            str,
            "A detailed description of the task for the subagent to perform autonomously. Include all necessary context and specify the expected output format.",
        ],
        subagent_type: Annotated[str, "The type of subagent to use. Must be one of the available agent types listed in the tool description."],
        runtime: ToolRuntime,
    ) -> str | Command:
        if subagent_type not in subagent_graphs:
            allowed_types = ", ".join([f"`{k}`" for k in subagent_graphs])
            return f"We cannot invoke subagent {subagent_type} because it does not exist, the only allowed types are {allowed_types}"
        if not runtime.tool_call_id:
            value_error_msg = "Tool call ID is required for subagent invocation"
            raise ValueError(value_error_msg)
        refused = _cap_refusal(cost_ledger, subagent_type)
        if refused is not None:
            return refused
        subagent, subagent_state = _validate_and_prepare_state(subagent_type, description, runtime)
        with _subagent_tracing_context():
            result = subagent.invoke(subagent_state)
        return _return_command_with_state_update(result, runtime.tool_call_id)

    async def atask(
        description: Annotated[
            str,
            "A detailed description of the task for the subagent to perform autonomously. Include all necessary context and specify the expected output format.",
        ],
        subagent_type: Annotated[str, "The type of subagent to use. Must be one of the available agent types listed in the tool description."],
        runtime: ToolRuntime,
    ) -> str | Command:
        if subagent_type not in subagent_graphs:
            allowed_types = ", ".join([f"`{k}`" for k in subagent_graphs])
            return f"We cannot invoke subagent {subagent_type} because it does not exist, the only allowed types are {allowed_types}"
        if not runtime.tool_call_id:
            value_error_msg = "Tool call ID is required for subagent invocation"
            raise ValueError(value_error_msg)
        refused = _cap_refusal(cost_ledger, subagent_type)
        if refused is not None:
            return refused
        subagent, subagent_state = _validate_and_prepare_state(subagent_type, description, runtime)
        with _subagent_tracing_context():
            result = await subagent.ainvoke(subagent_state)
        return _return_command_with_state_update(result, runtime.tool_call_id)

    return StructuredTool.from_function(
        name="task",
        func=task,
        coroutine=atask,
        description=description,
    )


class _DeprecatedKwargs(TypedDict, total=False):
    """TypedDict for deprecated SubAgentMiddleware keyword arguments.

    These arguments are deprecated and will be removed in version 0.5.0.
    Use `backend` and fully-specified `subagents` instead.
    """


class SubAgentMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """Middleware for providing subagents to an agent via a `task` tool.

    This middleware adds a `task` tool to the agent that can be used to invoke subagents.
    Subagents are useful for handling complex tasks that require multiple steps, or tasks
    that require a lot of context to resolve.

    A chief benefit of subagents is that they can handle multi-step tasks, and then return
    a clean, concise response to the main agent.

    Subagents are also great for different domains of expertise that require a narrower
    subset of tools and focus.

    Args:
        backend: Backend for file operations and execution. Required for the new API.
        subagents: List of fully-specified subagent configs. Each SubAgent
            must specify `model` and `tools`. Optional `interrupt_on` on
            individual subagents is respected.
        system_prompt: Instructions appended to main agent's system prompt
            about how to use the task tool.
        task_description: Custom description for the task tool.
        state_schema: Base graph state schema forwarded to `create_agent` when raw
            `SubAgent` specs are compiled. Leave unset to use `create_agent`'s
            default. `CompiledSubAgent` entries are unaffected — the caller owns
            those runnables' schemas.
        private_state_keys: `PrivateStateAttr`-marked state keys that must not cross
            the subagent boundary, in either direction. Build this with
            `subagent_private_state_keys(*schemas)` once the full middleware stack
            is known; it can also be assigned after construction via the
            `private_state_keys` property, which rebuilds the `task` tool.

    Example:
        ```python
        from bog_agents.middleware import SubAgentMiddleware
        from langchain.agents import create_agent

        agent = create_agent(
            "openai:gpt-4o",
            middleware=[
                SubAgentMiddleware(
                    backend=my_backend,
                    subagents=[
                        {
                            "name": "researcher",
                            "description": "Research agent",
                            "system_prompt": "You are a researcher.",
                            "model": "openai:gpt-4o",
                            "tools": [search_tool],
                        }
                    ],
                )
            ],
        )
        ```

    .. deprecated::
        The following arguments are deprecated and will be removed in version 0.5.0:
        `default_model`, `default_tools`, `default_middleware`,
        `default_interrupt_on`, `general_purpose_agent`. Use `backend` and `subagents` instead.
    """

    # Valid deprecated kwarg names for runtime validation
    _VALID_DEPRECATED_KWARGS = frozenset(
        {
            "default_model",
            "default_tools",
            "default_middleware",
            "default_interrupt_on",
            "general_purpose_agent",
        }
    )

    def __init__(
        self,
        *,
        backend: BackendProtocol | BackendFactory | None = None,
        subagents: list[SubAgent | CompiledSubAgent] | None = None,
        system_prompt: str | None = TASK_SYSTEM_PROMPT,
        task_description: str | None = None,
        state_schema: type | None = None,
        private_state_keys: frozenset[str] | None = None,
        cost_ledger: "CostLedger | None" = None,
        **deprecated_kwargs: Unpack[_DeprecatedKwargs],
    ) -> None:
        """Initialize the `SubAgentMiddleware`."""
        super().__init__()

        # Validate that only known deprecated kwargs are passed
        unknown_kwargs = set(deprecated_kwargs.keys()) - self._VALID_DEPRECATED_KWARGS
        if unknown_kwargs:
            msg = f"SubAgentMiddleware got unexpected keyword argument(s): {', '.join(sorted(unknown_kwargs))}"
            raise TypeError(msg)

        # Handle deprecated kwargs for backward compatibility
        default_model = deprecated_kwargs.get("default_model")
        default_tools = deprecated_kwargs.get("default_tools")
        default_middleware = deprecated_kwargs.get("default_middleware")
        default_interrupt_on = deprecated_kwargs.get("default_interrupt_on")
        # general_purpose_agent defaults to True if not specified
        general_purpose_agent = deprecated_kwargs.get("general_purpose_agent", True)

        # Warn about any deprecated kwargs that were provided
        provided_deprecated = [key for key in deprecated_kwargs if key != "general_purpose_agent"]
        if "general_purpose_agent" in deprecated_kwargs and not general_purpose_agent:
            provided_deprecated.append("general_purpose_agent")

        if provided_deprecated:
            warnings.warn(
                f"The following SubAgentMiddleware arguments are deprecated and will be removed "
                f"in version 0.5.0: {', '.join(provided_deprecated)}. "
                f"Use `backend` and fully-specified `subagents` instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        self._state_schema = state_schema
        self._private_state_keys = private_state_keys or frozenset()
        self._cost_ledger = cost_ledger
        self._task_description = task_description

        # Detect which API is being used
        using_new_api = backend is not None
        using_old_api = default_model is not None

        if using_old_api and not using_new_api:
            # Legacy API - build subagents from deprecated args
            self._subagent_specs = _get_subagents_legacy(
                default_model=default_model,
                default_tools=default_tools or [],
                default_middleware=default_middleware,
                default_interrupt_on=default_interrupt_on,
                subagents=subagents or [],
                general_purpose_agent=general_purpose_agent,
                state_schema=state_schema,
            )
        elif using_new_api:
            if not subagents:
                msg = "At least one subagent must be specified when using the new API"
                raise ValueError(msg)
            self._backend = backend
            self._subagents = subagents
            self._subagent_specs = self._get_subagents()
        else:
            msg = "SubAgentMiddleware requires either `backend` (new API) or `default_model` (deprecated API)"
            raise ValueError(msg)

        # Build system prompt with available agents
        if system_prompt and self._subagent_specs:
            agents_desc = "\n".join(f"- {s['name']}: {s['description']}" for s in self._subagent_specs)
            self.system_prompt = system_prompt + "\n\nAvailable subagent types:\n" + agents_desc
        else:
            self.system_prompt = system_prompt

        self._rebuild_task_tool()

    def _rebuild_task_tool(self) -> None:
        """(Re)build the `task` tool from the current specs and boundary filters.

        Called from `__init__` and from the `private_state_keys` setter — the tool
        closes over `private_state_keys`, so mutating the attribute alone would
        leave a stale filter in place.
        """
        self.tools = [
            _build_task_tool(
                self._subagent_specs,
                self._task_description,
                private_state_keys=self._private_state_keys,
                state_schema=self._state_schema,
                cost_ledger=self._cost_ledger,
            )
        ]

    @property
    def private_state_keys(self) -> frozenset[str]:
        """State keys withheld at both subagent state-crossing points."""
        return self._private_state_keys

    @private_state_keys.setter
    def private_state_keys(self, value: frozenset[str]) -> None:
        """Set the private-key filter and rebuild the `task` tool around it."""
        self._private_state_keys = value
        self._rebuild_task_tool()

    def _get_subagents(self) -> list[_SubagentSpec]:
        """Create runnable agents from specs.

        Returns:
            List of subagent specs with name, description, and runnable.
        """
        specs: list[_SubagentSpec] = []

        for spec in self._subagents:
            if "runnable" in spec:
                # CompiledSubAgent - use as-is
                compiled = cast("CompiledSubAgent", spec)
                specs.append({"name": compiled["name"], "description": compiled["description"], "runnable": compiled["runnable"]})
                continue

            # Raw SubAgent spec. `create_sub_agent` validates `model`/`tools`,
            # resolves the model, applies `interrupt_on`, and — crucially —
            # forwards the spec's `response_format`, which the previous inline
            # `create_agent` call dropped on the floor.
            specs.append(
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "runnable": create_sub_agent(spec, state_schema=self._state_schema),
                    "raw": spec,
                }
            )

        return specs

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Update the system message to include instructions on using subagents."""
        if self.system_prompt is not None:
            new_system_message = append_to_system_message(request.system_message, self.system_prompt)
            return handler(request.override(system_message=new_system_message))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        """(async) Update the system message to include instructions on using subagents."""
        if self.system_prompt is not None:
            new_system_message = append_to_system_message(request.system_message, self.system_prompt)
            return await handler(request.override(system_message=new_system_message))
        return await handler(request)
