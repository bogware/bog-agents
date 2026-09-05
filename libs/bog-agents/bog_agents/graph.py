"""Bog Agents come with planning, filesystem, and subagents."""

import dataclasses
import logging
import os
from collections.abc import Callable, Sequence
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Required, TypedDict, cast

if TYPE_CHECKING:
    from bog_agents.cost_ledger import CostLedger

from langchain.agents import AgentState, create_agent as _langchain_create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, InterruptOnConfig, TodoListMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ResponseFormat
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.cache.base import BaseCache
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from bog_agents._api.deprecation import warn_deprecated
from bog_agents._excluded_middleware import (
    _apply_excluded_middleware,
    _validate_excluded_middleware_config,
    _verify_excluded_middleware_coverage,
)
from bog_agents._messages_reducer import _messages_delta_reducer
from bog_agents._models import is_bedrock_model, resolve_model
from bog_agents._tools import _apply_tool_description_overrides
from bog_agents._version import __version__
from bog_agents.backends import StateBackend
from bog_agents.backends.protocol import BackendFactory, BackendProtocol
from bog_agents.feature_config import FeatureConfig
from bog_agents.middleware._tool_exclusion import _ToolExclusionMiddleware
from bog_agents.middleware.async_subagents import AsyncSubAgent, AsyncSubAgentMiddleware
from bog_agents.middleware.filesystem import FilesystemMiddleware
from bog_agents.middleware.memory import MemoryMiddleware
from bog_agents.middleware.output_truncation import OutputTruncationMiddleware
from bog_agents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from bog_agents.middleware.permissions import (
    FilesystemPermission,
    FilesystemPermissionsMiddleware,
    _build_interrupt_on_from_permissions,
)
from bog_agents.middleware.skills import SkillsMiddleware
from bog_agents.middleware.subagents import (
    DEFAULT_SUBAGENT_PROMPT,
    FORK_SUBAGENT_DESCRIPTION,
    GENERAL_PURPOSE_SUBAGENT,
    CompiledSubAgent,
    SubAgent,
    SubAgentMiddleware,
)
from bog_agents.middleware.summarization import (
    _BogAgentsSummarizationMiddleware,
    create_summarization_middleware,
)
from bog_agents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    _apply_profile_prompt,
    _harness_profile_for_model,
    _merge_profiles,
    named_harness_profile,
)
from bog_agents.token_audit import notify_assembly


class DeepAgentState(AgentState):
    """`AgentState` with a `DeltaChannel` reducer on the `messages` key.

    The delta reducer keeps checkpoint growth linear (O(N)) instead of
    quadratic (O(N**2)) as the message history grows, by snapshotting the full
    list only every `snapshot_frequency` writes and storing deltas in between.

    This is opt-in for `create_agent` — pass `state_schema=DeepAgentState` (or a
    subclass) to enable it. The deepagents-compatible `create_deep_agent`
    wrapper uses it by default. Subclass this (not `AgentState`) for custom
    state so the reducer is preserved:

    ```python
    from bog_agents import DeepAgentState


    class MyState(DeepAgentState):
        page_url: str
    ```
    """

    messages: Required[Annotated[list[AnyMessage], DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)]]  # ty: ignore[invalid-argument-type]


class SystemPromptConfig(TypedDict, total=False):
    """Structured `system_prompt` for `create_agent`.

    All keys are optional. Each accepts a `str` or a `SystemMessage` (to
    carry explicit `cache_control` markers), and `base` additionally accepts
    `None` to drop the base prompt entirely.
    """

    prefix: str | SystemMessage | None
    """Text placed before the base prompt."""

    base: str | SystemMessage | None
    """Replacement for the built-in base prompt.

    Omit the key to keep the built-in base (or the active
    `HarnessProfile.base_system_prompt`). Set it to `None` to drop the base
    entirely, leaving only `prefix`, `suffix`, and middleware-contributed
    content.
    """

    suffix: str | SystemMessage | None
    """Text placed after the base prompt (before any profile suffix)."""


_PROMPT_SEPARATOR = "\n\n"


def _price_tokens(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD for a call from the cost catalog, or `None` when the model is unpriced (ROADMAP #74)."""
    from bog_agents.middleware.cost_tracker import price_for_model

    prices = price_for_model(model.split(":", 1)[1] if ":" in model else model)
    if prices is None:
        return None
    return (input_tokens / 1_000_000) * prices[0] + (output_tokens / 1_000_000) * prices[1]


def _fork_system_prompt(system_prompt: Any, profile: Any) -> str | SystemMessage:
    """The parent's assembled prompt (prefix, base, suffix, profile suffix) for the built-in `fork` subagent (ROADMAP #71)."""
    cfg = _normalize_system_prompt(system_prompt)
    parts: list[str | SystemMessage] = []
    prefix = cfg.get("prefix")
    if prefix is not None:
        parts.append(prefix)
    profile_base = profile.base_system_prompt if profile.base_system_prompt is not None else BASE_AGENT_PROMPT
    base = cfg.get("base", profile_base)
    if base is not None:
        parts.append(base)
    suffix = cfg.get("suffix")
    if suffix is not None:
        parts.append(suffix)
    if profile.system_prompt_suffix is not None:
        parts.append(profile.system_prompt_suffix)
    return _assemble_prompt_parts(parts)


def _assemble_prompt_parts(parts: list[str | SystemMessage]) -> str | SystemMessage:
    r"""Join prompt parts into a single `str` or `SystemMessage`.

    All-`str` parts join with blank lines. If any part is a `SystemMessage`,
    the result is a `SystemMessage` whose `content_blocks` concatenate each
    part's blocks with `\n\n` separators, preserving `cache_control` markers.

    Args:
        parts: Ordered prompt fragments to concatenate.

    Returns:
        The joined prompt as a plain `str` (when every part is a `str`) or a
        `SystemMessage` (when any part carries content blocks).
    """
    if not parts:
        return ""
    if all(isinstance(part, str) for part in parts):
        return _PROMPT_SEPARATOR.join(cast("list[str]", parts))
    blocks: list[Any] = []
    for i, part in enumerate(parts):
        if i:
            blocks.append({"type": "text", "text": _PROMPT_SEPARATOR})
        if isinstance(part, SystemMessage):
            blocks.extend(part.content_blocks)
        else:
            blocks.append({"type": "text", "text": part})
    return SystemMessage(content_blocks=blocks)


def _normalize_system_prompt(
    system_prompt: str | SystemMessage | SystemPromptConfig | None,
) -> SystemPromptConfig:
    """Coerce the `system_prompt` argument into a `SystemPromptConfig`.

    `None` becomes an empty config; a bare `str`/`SystemMessage` becomes a
    `prefix` (matching the historical behavior of placing caller text before
    the base); a config dict is returned unchanged.

    Args:
        system_prompt: The caller-supplied `system_prompt` value.

    Returns:
        A `SystemPromptConfig` mapping with the caller's intent normalized.
    """
    if system_prompt is None:
        return {}
    if isinstance(system_prompt, (str, SystemMessage)):
        return {"prefix": system_prompt}
    return system_prompt


def _apply_custom_middleware(
    base: list[AgentMiddleware],
    custom: Sequence[AgentMiddleware],
    *,
    core_names: set[str] | None = None,
) -> list[AgentMiddleware]:
    """Merge custom middleware into `base` by `.name`, replacing collisions in place.

    A custom middleware whose `.name` matches a middleware still present in
    `base` REPLACES that built-in at its original position (rather than being
    dropped by keep-first dedup). Brand-new custom middleware lands after the
    last `core_names` member — so it precedes the profile/prompt-caching/memory
    tail — or at the end when `core_names` is unset.

    Args:
        base: The assembled base stack (not mutated).
        custom: Caller-supplied middleware to merge in.
        core_names: Names of the core stack, used to position brand-new custom
            middleware ahead of the tail. When `None`, new middleware is
            appended at the end.

    Returns:
        A new list with collisions replaced in place and novel entries spliced
        in after the core stack.
    """
    if not custom:
        return list(base)
    current_names = {m.name for m in base}
    replacements: dict[str, AgentMiddleware] = {}
    to_append: list[AgentMiddleware] = []
    for m in custom:
        if m.name in current_names:
            replacements[m.name] = m
        else:
            to_append.append(m)
    result = list(base)
    for i, m in enumerate(result):
        if m.name in replacements:
            result[i] = replacements[m.name]
    if to_append and core_names is not None:
        # Land new middleware after the last core entry, ahead of the tail.
        pos = max((i for i, m in enumerate(result) if m.name in core_names), default=len(result) - 1) + 1
        result[pos:pos] = to_append
    else:
        result.extend(to_append)
    return result


def _create_bedrock_prompt_caching_middleware() -> AgentMiddleware[Any, Any, Any] | None:
    """Create Bedrock prompt-caching middleware when `langchain-aws` is installed.

    `langchain-aws` is an optional dependency, so its prompt-caching submodule
    is imported lazily. When the package (or that submodule) is absent the
    function degrades gracefully by returning `None`, leaving a Bedrock model to
    pay full input-token price rather than crashing the agent build. Import
    errors that name an unrelated transitive dependency are re-raised so a
    genuinely broken `langchain-aws` install is not silently masked.

    Returns:
        A `BedrockPromptCachingMiddleware` instance configured to ignore
        unsupported models, or `None` when `langchain-aws` is unavailable.
    """
    module_name = "langchain_aws.middleware.prompt_caching"
    try:
        module = import_module(module_name)
    except ImportError as exc:
        if exc.name not in {"langchain_aws", "langchain_aws.middleware", module_name}:
            raise
        return None
    middleware_cls = module.BedrockPromptCachingMiddleware
    return cast("AgentMiddleware[Any, Any, Any]", middleware_cls(unsupported_model_behavior="ignore"))


def _append_prompt_caching_middleware(
    middleware: list[AgentMiddleware[Any, Any, Any]],
    resolved_model: str | BaseChatModel,
) -> None:
    """Append the provider-appropriate prompt-caching tail for `resolved_model`.

    Always appends `AnthropicPromptCachingMiddleware` (a no-op for non-Anthropic
    models via `unsupported_model_behavior="ignore"`, so the non-Bedrock stack is
    byte-identical to before). When `resolved_model` targets AWS Bedrock and
    `langchain-aws` is installed, `BedrockPromptCachingMiddleware` is appended
    directly after it so Bedrock turns pay the cached input-token price. The two
    never both fire: the Anthropic middleware ignores Bedrock (`ChatBedrock`)
    models, and the Bedrock entry is only added when the model is Bedrock. The
    Bedrock entry is the innermost tail so, like the Anthropic one, it sees the
    final message list after summarization/memory transforms.

    Args:
        middleware: The stack to append onto; mutated in place.
        resolved_model: The already-resolved model this stack will serve.
    """
    middleware.append(AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"))
    if is_bedrock_model(resolved_model):
        bedrock_middleware = _create_bedrock_prompt_caching_middleware()
        if bedrock_middleware is not None:
            middleware.append(bedrock_middleware)


def _merge_fs_interrupt_on(
    fs_interrupt_on: dict[str, InterruptOnConfig],
    user_interrupt_on: dict[str, bool | InterruptOnConfig] | None,
) -> dict[str, bool | InterruptOnConfig] | None:
    """Merge filesystem-permission-derived interrupt configs with user configs.

    User-supplied entries override generated ones per tool name. Returns `None`
    when both inputs are empty so callers can skip installing
    `HumanInTheLoopMiddleware`.

    Args:
        fs_interrupt_on: Interrupt configs synthesized from `interrupt`-mode
            filesystem permission rules.
        user_interrupt_on: Caller-supplied `interrupt_on` mapping, if any.

    Returns:
        The merged mapping, or `None` when there is nothing to interrupt on.
    """
    if not fs_interrupt_on and not user_interrupt_on:
        return None
    merged: dict[str, bool | InterruptOnConfig] = {**fs_interrupt_on}
    if user_interrupt_on:
        merged.update(user_interrupt_on)
    return merged


BASE_AGENT_PROMPT = """You are a Bog Agents agent, an AI assistant that helps users accomplish tasks using tools. You respond with text and tool calls. The user can see your responses and tool outputs in real time.

## Core Behavior

- Be concise and direct. Don't over-explain unless asked.
- NEVER add unnecessary preamble (\"Sure!\", \"Great question!\", \"I'll now...\").
- Don't say \"I'll now do X\" — just do it.
- If the request is underspecified, ask only the minimum follow-up needed to take the next useful action.
- If asked how to approach something, explain first, then act.

## Professional Objectivity

- Prioritize accuracy over validating the user's beliefs
- Disagree respectfully when the user is incorrect
- Avoid unnecessary superlatives, praise, or emotional validation

## Doing Tasks

When the user asks you to do something:

1. **Understand first** — read relevant files, check existing patterns. Quick but thorough — gather enough evidence to start, then iterate.
2. **Act** — implement the solution. Work quickly but accurately.
3. **Verify** — check your work against what was asked, not against your own output. Your first attempt is rarely correct — iterate.

Keep working until the task is fully complete. Don't stop partway and explain what you would do — just do it. Only yield back to the user when the task is done or you're genuinely blocked.

**When things go wrong:**
- If something fails repeatedly, stop and analyze *why* — don't keep retrying the same approach.
- If you're blocked, tell the user what's wrong and ask for guidance.

## Clarifying Requests

- Do not ask for details the user already supplied.
- Use reasonable defaults when the request clearly implies them.
- Prioritize missing semantics like content, delivery, detail level, or alert criteria.
- Avoid opening with a long explanation of tool, scheduling, or integration limitations when a concise blocking follow-up question would move the task forward.
- Ask domain-defining questions before implementation questions.
- For monitoring or alerting requests, ask what signals, thresholds, or conditions should trigger an alert.

## Progress Updates

For longer tasks, provide brief progress updates at reasonable intervals — a concise sentence recapping what you've done and what's next."""  # noqa: E501


_PROVENANCE_LOOP_PROMPT = """## Citations & Verification (D-5 provenance loop)

You have access to provenance tools provided by the citations,
hallucination_detection, and fact_check middleware. Use them by default
so every factual claim you emit carries traceable evidence:

1. **Register your sources.** When you read a file, fetch a web page,
   or pull data from a database, call ``register_data_source`` (or the
   equivalent for the active middleware) with the source's type, name,
   and excerpt. Do this BEFORE you cite from it.
2. **Cite inline.** When you make a factual claim drawn from a source,
   call ``add_citation`` (or your output should include bracket
   citations like ``[1]``, ``[2]``) that map to registered sources.
   Label each citation's relationship as ``supports``,
   ``contradicts``, or ``mentions``.
3. **Verify numbers.** When you produce a numerical claim
   (statistic, percentage, dollar amount, date), call
   ``register_fact`` and ``verify_claim`` so the
   hallucination-detection middleware can flag unsourced or
   contradicted numbers before they reach the user.
4. **Submit uncertain claims for fact-checking.** When you're less
   than confident about a claim or when you couldn't find a primary
   source, use ``submit_claim`` to log it for follow-up review rather
   than asserting it.
5. **Surface the bibliography.** At the end of substantive responses,
   call the active middleware's ``generate_bibliography`` /
   ``verification_report`` / ``factcheck_report`` so the user can see
   the evidence trail at a glance.

When you cannot find a source for a claim, SAY SO explicitly rather
than guessing. Unsourced claims marked as such are far more useful
than confident-sounding hallucinations.
"""


def get_default_model() -> ChatAnthropic:
    """Get the default model for bog-agents agents.

    Routes through `resolve_model` so the long-running read timeout from
    `BOG_AGENTS_MODEL_READ_TIMEOUT` (default 3600s) is applied. A bare
    `ChatAnthropic(...)` would inherit the SDK's stock 600s default and
    cut off long thinking/streaming turns mid-stream.

    Returns:
        `ChatAnthropic` instance configured with Claude Sonnet 4.6.
    """
    return cast(ChatAnthropic, resolve_model("anthropic:claude-sonnet-4-6"))


def _dedup_middleware_by_name(middleware_list: list[AgentMiddleware]) -> list[AgentMiddleware]:
    """Drop duplicate middleware instances that share a `.name`, keeping the first.

    langchain refuses to compile an agent whose middleware list contains two
    instances with the same `.name`, raising an opaque "Please remove duplicate
    middleware instances" `AssertionError` that points at the caller's list
    rather than the framework-injected twin. The per-feature construction guards
    in `create_agent` are the primary defense (they honor user precedence by not
    building the twin in the first place); this pass is a keep-first backstop for
    any combination they miss.

    Args:
        middleware_list: Ordered list of middleware instances to de-duplicate.

    Returns:
        A new list with later duplicates (by `.name`) removed, order otherwise
        preserved. The first occurrence of each name is kept.
    """
    seen_names: set[str] = set()
    deduped: list[AgentMiddleware] = []
    dropped: list[str] = []
    for mw in middleware_list:
        name = getattr(mw, "name", type(mw).__name__)
        if name in seen_names:
            dropped.append(name)
            continue
        seen_names.add(name)
        deduped.append(mw)
    if dropped:
        logging.getLogger(__name__).warning(
            "Removed duplicate middleware instance(s) with name(s) %s; the first occurrence of each was kept. "
            "This usually means the same feature was supplied both via a convenience kwarg and via middleware=.",
            sorted(set(dropped)),
        )
    return deduped


def _validate_middleware_ordering(middleware_list: list[AgentMiddleware]) -> None:
    """Raise ValueError if any middleware's requirements appear later in the stack.

    Iterates the middleware list in order, tracking which types have been seen.
    If a middleware declares `requires = [SomeMiddleware]` and `SomeMiddleware`
    has not yet appeared in the list, a `ValueError` is raised.

    Args:
        middleware_list: Ordered list of middleware instances to validate.

    Raises:
        ValueError: If a required middleware type appears after the middleware
            that depends on it, or is missing entirely from the stack.
    """
    seen: set[type] = set()
    for mw in middleware_list:
        for req in getattr(type(mw), "requires", []):
            if req not in seen:
                provided = [type(m).__name__ for m in middleware_list]
                msg = f"{type(mw).__name__} requires {req.__name__} to appear earlier in the middleware stack. Current order: {provided}"
                raise ValueError(msg)
        seen.add(type(mw))


_FEATURE_CONFIG_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in dataclasses.fields(FeatureConfig))


def _resolve_feature_config(
    *,
    config: FeatureConfig | None,
    features: FeatureConfig | None,
    legacy_flags: dict[str, Any],
) -> FeatureConfig:
    """Pick the right ``FeatureConfig`` for a ``create_agent`` call.

    ``FeatureConfig`` is the single source of truth for feature flags. This
    helper consolidates the three legacy entry points:

    1. ``config=FeatureConfig(...)`` — preferred.
    2. ``features=FeatureConfig(...)`` — deprecated alias of ``config``.
    3. Bare ``enable_*`` / feature kwargs caught by ``**legacy_flags``.

    A ``DeprecationWarning`` is emitted when the legacy kwarg surface is
    used so callers can migrate. Unknown keys raise ``TypeError`` so a
    typo doesn't silently fall through.
    """
    # ``config`` is the preferred name; ``features`` is a deprecated alias.
    # When both are provided, ``config`` wins (preserving long-standing
    # behaviour) and ``features`` is silently ignored.
    explicit = config if config is not None else features

    if not legacy_flags:
        return explicit if explicit is not None else FeatureConfig()

    unknown = sorted(set(legacy_flags) - _FEATURE_CONFIG_FIELD_NAMES)
    if unknown:
        msg = f"create_agent() got unexpected keyword argument(s): {unknown}"
        raise TypeError(msg)

    import warnings as _warnings

    # P1-6: This deprecation shim will be removed in bog-agents 1.0.
    # Users still hitting it should migrate to ``config=FeatureConfig(...)``
    # — the kwarg backdoor makes ``FeatureConfig`` evolution backwards-
    # incompatible (renaming a field silently rejects what used to be
    # valid kwargs). When you bump the major version, delete the
    # ``**legacy_feature_flags`` parameter and the call to
    # ``_resolve_feature_config`` that supplies it, and raise immediately
    # on any unknown kwargs.
    _warnings.warn(
        "Passing individual feature flags as kwargs to create_agent() is "
        "deprecated and will be removed in bog-agents 1.0; pass "
        "`config=FeatureConfig(...)` instead. Affected "
        f"flags: {sorted(legacy_flags)}",
        DeprecationWarning,
        stacklevel=3,
    )

    if explicit is None:
        return FeatureConfig(**legacy_flags)
    # Merge: explicit FeatureConfig + legacy flag overrides on top.
    return dataclasses.replace(explicit, **legacy_flags)


def create_agent(  # Complex graph assembly logic with many conditional branches
    model: str | BaseChatModel | None = None,
    tools: Sequence[BaseTool | Callable | dict[str, Any]] | None = None,
    *,
    system_prompt: str | SystemMessage | SystemPromptConfig | None = None,
    middleware: Sequence[AgentMiddleware] = (),
    subagents: list[SubAgent | CompiledSubAgent | AsyncSubAgent] | None = None,
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    response_format: ResponseFormat | None = None,
    context_schema: type[Any] | None = None,
    checkpointer: Checkpointer | None = None,
    store: BaseStore | None = None,
    backend: BackendProtocol | BackendFactory | None = None,
    interrupt_on: dict[str, bool | InterruptOnConfig] | None = None,
    permissions: list[FilesystemPermission] | None = None,
    guardrails: Sequence[Any] | None = None,
    state_schema: type[Any] | None = None,
    debug: bool = False,
    name: str | None = None,
    cache: BaseCache | None = None,
    config: FeatureConfig | None = None,
    features: FeatureConfig | None = None,
    max_turns: int = 200,
    cost_ledger: "CostLedger | None" = None,
    **legacy_feature_flags: Any,
) -> CompiledStateGraph:
    """Create a bog-agents agent.

    !!! warning "Bog Agents agents require a LLM that supports tool calling!"

    ## Middleware execution order

    Middleware runs in **declaration order** within the stack. The list below
    is the canonical *order* built-in middleware occupy when active — it is
    **not** an "always on" default set. Which middleware are actually composed
    depends on the ``FeatureConfig`` / arguments you pass; several entries below
    (notably ``LifecycleHooksMiddleware``, ``HttpHooksMiddleware``,
    ``LangSmithMiddleware``, ``ExpertRulesMiddleware``,
    ``RulesMiddleware``, ``ContextPackingMiddleware``, ``ThinkingMiddleware``,
    and ``IntelligentCompactionMiddleware``) are opt-in and only present when
    you enable them or pass them via ``middleware=``. The canonical order is:

    1. Lifecycle / observability — ``LifecycleHooksMiddleware``,
       ``HttpHooksMiddleware``, ``LangSmithMiddleware``,
       ``AuditTrailMiddleware``
    2. Pre-prompt safety — ``DLPMiddleware``, ``RBACMiddleware``,
       ``ApprovalGatesMiddleware``, ``ExpertRulesMiddleware`` (selective
       auto-approval is data-driven via ``SafeToolsConfig`` / ``is_tool_safe``
       from ``bog_agents.middleware.safe_tools`` — there is no
       ``SafeToolsMiddleware`` class)
    3. Context preparation — ``RulesMiddleware``, ``MemoryMiddleware``,
       ``SkillsMiddleware``, ``RepoMapMiddleware``,
       ``CodeIntelligenceMiddleware``, ``ContextPackingMiddleware``
    4. Tool & state surfaces — ``FilesystemMiddleware``,
       ``GitToolsMiddleware``, ``WorktreeMiddleware``,
       ``SubAgentMiddleware``, ``PlanModeMiddleware``,
       ``ThinkingMiddleware``, ``CheckpointingMiddleware``
    5. Token / cost management — ``SummarizationMiddleware``,
       ``IntelligentCompactionMiddleware``, ``CostTrackerMiddleware``
    6. Anything you pass via ``middleware=`` — appended at the very end.

    The order matters for soft conflicts the static
    ``_validate_middleware_ordering`` check can't catch. For example
    ``DLPMiddleware`` must run before ``AuditTrailMiddleware`` if you want
    redacted values to land in audit logs; both fire in pre-prompt safety,
    in declaration order. When adding new built-in middleware, place it
    in the section that matches its concern. When passing user middleware
    via ``middleware=`` you control its position relative to the defaults
    only by being appended after them; an ``after=`` / ``before=`` API is
    a deliberate TODO.

    By default, this agent has access to the following tools:

    - `write_todos`: manage a todo list
    - `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`: file operations
    - `execute`: run shell commands
    - `task`: call subagents

    The `execute` tool allows running shell commands if the backend implements `SandboxBackendProtocol`.
    For non-sandbox backends, the `execute` tool will return an error message.

    Args:
        model: The model to use.

            Defaults to `claude-sonnet-4-6`.

            Use the `provider:model` format (e.g., `openai:gpt-5`) to quickly switch between models.

            If an `openai:` model is used, the agent will use the OpenAI
            Responses API by default. To use OpenAI chat completions instead,
            initialize the model with
            `init_chat_model("openai:...", use_responses_api=False)` and pass
            the initialized model instance here. To disable data retention with
            the Responses API, use
            `init_chat_model("openai:...", use_responses_api=True, store=False, include=["reasoning.encrypted_content"])`
            and pass the initialized model instance here.
        tools: The tools the agent should have access to.

            In addition to custom tools you provide, bog-agents agents include built-in tools for planning,
            file management, and subagent spawning.
        system_prompt: Custom system instructions to prepend before the base bog-agents agent
            prompt.

            If a string, it's concatenated with the base prompt.
        middleware: Additional middleware to apply after the standard middleware stack
            (`TodoListMiddleware`, `FilesystemMiddleware`, `SubAgentMiddleware`,
            `SummarizationMiddleware`, `AnthropicPromptCachingMiddleware`,
            `PatchToolCallsMiddleware`).
        subagents: The subagents to use.

            Each subagent should be a `dict` with the following keys:

            - `name`
            - `description` (used by the main agent to decide whether to call the sub agent)
            - `system_prompt` (used as the system prompt in the subagent)
            - (optional) `tools`
            - (optional) `model` (either a `LanguageModelLike` instance or `dict` settings)
            - (optional) `middleware` (list of `AgentMiddleware`)
        skills: Optional list of skill source paths (e.g., `["/skills/user/", "/skills/project/"]`).

            Paths must be specified using POSIX conventions (forward slashes) and are relative
            to the backend's root. When using `StateBackend` (default), provide skill files via
            `invoke(files={...})`. With `FilesystemBackend`, skills are loaded from disk relative
            to the backend's `root_dir`. Later sources override earlier ones for skills with the
            same name (last one wins).
        memory: Optional list of memory file paths (`AGENTS.md` files) to load
            (e.g., `["/memory/AGENTS.md"]`).

            Display names are automatically derived from paths.

            Memory is loaded at agent startup and added into the system prompt.
        response_format: A structured output response format to use for the agent.
        context_schema: The schema of the bog-agents agent.
        checkpointer: Optional `Checkpointer` for persisting agent state between runs.
        store: Optional store for persistent storage (required if backend uses `StoreBackend`).
        backend: Optional backend for file storage and execution.

            Pass either a `Backend` instance or a callable factory like `lambda rt: StateBackend(rt)`.
            For execution support, use a backend that implements `SandboxBackendProtocol`.
        interrupt_on: Mapping of tool names to interrupt configs.

            Pass to pause agent execution at specified tool calls for human approval or modification.

            Example: `interrupt_on={"edit_file": True}` pauses before every edit.
        permissions: Optional list of `FilesystemPermission` rules applied to the
            built-in filesystem tools for the main agent and its subagents.

            Rules are evaluated in declaration order; the first match wins, and
            an unmatched call is allowed. Each rule's `mode` can be `"allow"`
            (proceed), `"deny"` (the tool returns a permission-denied error), or
            `"interrupt"` (the call pauses for human approval via
            `HumanInTheLoopMiddleware`). Deny enforcement is exact and
            path-aware; interrupt-mode rules install HITL on the affected
            filesystem tools. Subagents inherit these rules unless their spec
            sets its own `permissions`, which replaces the parent rules.
        state_schema: Optional custom state schema for the agent graph. Pass
            `DeepAgentState` (or a subclass) to enable the `DeltaChannel`
            messages reducer for O(N) checkpoint growth. When `None` (default),
            the underlying LangChain default state is used, preserving existing
            behavior.
        debug: Whether to enable debug mode. Passed through to `create_agent`.
        name: The name of the agent. Passed through to `create_agent`.
        cache: The cache to use for the agent. Passed through to `create_agent`.
        cost_ledger: Session `CostLedger` whose `RunawayCaps` gate every
            `task` / async subagent spawn and total spend (v6 SDK-7). `None`
            leaves the default fan-out path uncounted and uncapped.
        config: Primary path for feature configuration via `FeatureConfig`.

            Pass a `FeatureConfig` instance to configure all feature flags in one
            place instead of using 90+ individual kwargs. Individual kwargs take
            precedence over `config` when both are provided.

            Example::

                from bog_agents import FeatureConfig, create_agent

                agent = create_agent(
                    config=FeatureConfig(enable_git_tools=True, enable_cost_tracking=True)
                )
        features: Alias for `config`, kept for backwards compatibility.

    Returns:
        A configured bog-agents agent.
    """
    f = _resolve_feature_config(config=config, features=features, legacy_flags=legacy_feature_flags)

    # Capture the raw model spec string (if any) before it is resolved to a
    # `BaseChatModel`, so a registered `HarnessProfile` can be looked up by the
    # exact `provider:model` key the caller passed.
    _model_spec: str | None = model if isinstance(model, str) else None

    if model is None:
        # Relying on the default model is a deprecated path (parity with
        # deepagents 0.5.3): callers should construct their model explicitly.
        warn_deprecated(
            since="0.7.0",
            removal="1.0.0",
            message=(
                "Passing `model=None` to `create_agent` is deprecated and will "
                "be removed in bog-agents==1.0.0. The `model` parameter type "
                "will change from `BaseChatModel | str | None` to "
                "`BaseChatModel | str`. Specify a model explicitly "
                "(e.g., `ChatAnthropic(model_name=...)` or "
                "`create_agent(model='anthropic:claude-sonnet-4-6')`)."
            ),
            package="bog-agents",
        )
        _api_key_vars = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")
        if not any(os.environ.get(k) for k in _api_key_vars):
            import warnings

            warnings.warn(
                "No API key found. The default model (Claude Sonnet) requires ANTHROPIC_API_KEY.\n"
                "Set it before calling agent.invoke():\n"
                "  export ANTHROPIC_API_KEY='sk-ant-...'\n"
                "Or pass a different model: create_agent(model='openai:gpt-4o')",
                UserWarning,
                stacklevel=2,
            )
        model = get_default_model()
    else:
        model = resolve_model(model)

    # Resolve the harness profile for this model (exact `provider:model` key,
    # then provider prefix). Returns a null-object default when none is
    # registered, so the prompt overlay and extra-middleware application below
    # are no-ops out of the box.
    _profile = _harness_profile_for_model(model, _model_spec)
    if f.harness_profile:
        # ROADMAP #54: a named profile (`lean`, or anything registered) layers
        # over the model's own so provider guidance survives and the named
        # profile's prompt / tool descriptions / exclusions win.
        _profile = _merge_profiles(_profile, named_harness_profile(f.harness_profile))

    # Apply harness-profile tool-description overrides up front, producing a
    # copied tool list (caller-owned tools are never mutated). No-op when the
    # profile registers no overrides, so `_tools` mirrors `tools` by default.
    _tools = _apply_tool_description_overrides(tools, _profile.tool_description_overrides)

    # Required scaffolding that `HarnessProfile.excluded_middleware` may never
    # strip. Validated up front; coverage of every exclusion is verified after
    # all stacks are assembled. The matched-sets accumulate across the main
    # agent, the general-purpose subagent, and declarative subagents.
    _required_mw_classes: frozenset[type[AgentMiddleware[Any, Any, Any]]] = frozenset({FilesystemMiddleware, SubAgentMiddleware})
    _required_mw_names: frozenset[str] = frozenset({"FilesystemMiddleware", "SubAgentMiddleware"})
    _validate_excluded_middleware_config(_profile, required_classes=_required_mw_classes, required_names=_required_mw_names)
    _excl_matched_classes: set[type[AgentMiddleware[Any, Any, Any]]] = set()
    _excl_matched_names: set[str] = set()

    # ``StateBackend`` is a class that doubles as a ``BackendFactory``
    # (i.e. callable taking a ToolRuntime). Pass the class itself so
    # downstream code which expects ``BackendProtocol | BackendFactory``
    # gets a real factory rather than a typo'd grouping expression.
    backend = backend if backend is not None else StateBackend

    # Process user-provided subagents to fill in defaults for model, tools, and middleware
    processed_subagents: list[SubAgent | CompiledSubAgent] = []
    async_subagents: list[AsyncSubAgent] = []
    for spec in subagents or []:
        # Runtime sanity-check: TypedDicts give static guarantees only. Without
        # this, a typo like `descripton=...` silently produces a subagent the
        # main agent can't usefully describe to the user, and the failure
        # surfaces much later as a confusing tool-call error.
        if not isinstance(spec, dict):
            msg = f"subagents entries must be dicts; got {type(spec).__name__}"
            raise TypeError(msg)
        if "runnable" not in spec and "graph_id" not in spec:
            missing = [k for k in ("name", "description", "system_prompt") if k not in spec]
            if missing:
                msg = f"subagent {spec.get('name', '<unnamed>')!r} is missing required keys: {missing}"
                raise ValueError(msg)
        if "graph_id" in spec:
            async_subagents.append(cast("AsyncSubAgent", spec))
            continue
        if "runnable" in spec:
            # CompiledSubAgent - use as-is
            processed_subagents.append(spec)
        else:
            # SubAgent - fill in defaults and prepend base middleware.
            # Capture the raw model spec string BEFORE `resolve_model` so this
            # subagent resolves its OWN harness profile (a subagent on a
            # different model no longer silently inherits the parent's profile).
            raw_subagent_model = spec.get("model", model)
            subagent_model = resolve_model(raw_subagent_model)
            _subagent_spec = raw_subagent_model if isinstance(raw_subagent_model, str) else None
            _subagent_profile = _harness_profile_for_model(subagent_model, _subagent_spec)
            _validate_excluded_middleware_config(
                _subagent_profile,
                required_classes=_required_mw_classes,
                required_names=_required_mw_names,
            )
            _sub_matched_classes: set[type[AgentMiddleware[Any, Any, Any]]] = set()
            _sub_matched_names: set[str] = set()

            # Resolve permissions: a subagent's own rules replace the parent's
            # entirely; otherwise it inherits the top-level rules.
            subagent_permissions = spec.get("permissions", permissions)

            # Build middleware: base stack + skills (if specified) + user's middleware
            subagent_middleware: list[AgentMiddleware[Any, Any, Any]] = [
                TodoListMiddleware(),
                FilesystemMiddleware(
                    backend=backend,
                    custom_tool_descriptions=_subagent_profile.tool_description_overrides,
                    _permissions=subagent_permissions,
                ),
            ]
            if subagent_permissions:
                subagent_middleware.append(FilesystemPermissionsMiddleware(permissions=subagent_permissions))
            subagent_middleware.extend(
                [
                    create_summarization_middleware(subagent_model, backend),
                    PatchToolCallsMiddleware(),
                ]
            )
            subagent_skills = spec.get("skills")
            if subagent_skills:
                subagent_middleware.append(SkillsMiddleware(backend=backend, sources=subagent_skills))
            # Core names captured before the profile/prompt-caching tail so spec
            # middleware colliding with a built-in REPLACES it in place, and
            # novel spec middleware splices in ahead of the tail.
            _subagent_core_names = {m.name for m in subagent_middleware}
            subagent_middleware.extend(_subagent_profile.materialize_extra_middleware())
            if _subagent_profile.excluded_tools:
                subagent_middleware.append(_ToolExclusionMiddleware(excluded=_subagent_profile.excluded_tools))
            _append_prompt_caching_middleware(subagent_middleware, subagent_model)
            subagent_middleware = _apply_excluded_middleware(
                subagent_middleware,
                _subagent_profile,
                matched_classes=_sub_matched_classes,
                matched_names=_sub_matched_names,
            )
            subagent_middleware = _apply_custom_middleware(
                subagent_middleware,
                spec.get("middleware", []),
                core_names=_subagent_core_names,
            )
            subagent_middleware = _apply_excluded_middleware(
                subagent_middleware,
                _subagent_profile,
                matched_classes=_sub_matched_classes,
                matched_names=_sub_matched_names,
            )
            _verify_excluded_middleware_coverage(
                _subagent_profile,
                _sub_matched_classes,
                _sub_matched_names,
                required_classes=_required_mw_classes,
                required_names=_required_mw_names,
            )

            subagent_interrupt_on = _merge_fs_interrupt_on(
                _build_interrupt_on_from_permissions(subagent_permissions or []),
                spec.get("interrupt_on", interrupt_on),
            )

            # Prepend the anti-fabrication preamble so every subagent (custom
            # or built-in) inherits the same honesty rules as the general-
            # purpose agent. Subagents commonly omit shell access in their
            # AGENTS.md but still get told "run npm test"; without the rule
            # they hallucinate the output.
            user_prompt = spec.get("system_prompt", "") or ""
            # A subagent inherits the main tools unless it declares its own;
            # apply this subagent's harness tool-description overrides to
            # whichever set applies.
            raw_subagent_tools = spec.get("tools") if "tools" in spec else tools
            subagent_tools = _apply_tool_description_overrides(raw_subagent_tools, _subagent_profile.tool_description_overrides)
            # Compose the anti-fabrication preamble with the authored prompt,
            # then layer this subagent's profile overlay (base replacement
            # and/or suffix) on top so a model-specific profile applies here too.
            _subagent_base_prompt = f"{DEFAULT_SUBAGENT_PROMPT}\n\n{user_prompt}".strip()
            processed_spec: SubAgent = {  # ty: ignore[missing-typed-dict-key]
                **spec,
                "model": subagent_model,
                "tools": subagent_tools or [],
                "middleware": subagent_middleware,
                "system_prompt": _apply_profile_prompt(_subagent_profile, _subagent_base_prompt),
            }
            if subagent_interrupt_on is not None:
                processed_spec["interrupt_on"] = subagent_interrupt_on
            processed_subagents.append(processed_spec)

    # Auto-add the default general-purpose subagent unless the caller already
    # supplied their own (an explicit `general-purpose` spec is how you
    # override/configure it) or the active harness profile disables it via
    # `GeneralPurposeSubagentProfile(enabled=False)`. When the GP subagent is
    # suppressed and no other synchronous subagents remain, the
    # `SubAgentMiddleware` install below is skipped entirely — dropping the
    # `task` tool.
    gp_profile = _profile.general_purpose_subagent or GeneralPurposeSubagentProfile()
    _user_has_gp = any(spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"] for spec in processed_subagents)
    all_subagents: list[SubAgent | CompiledSubAgent]
    if gp_profile.enabled is not False and not _user_has_gp:
        gp_middleware: list[AgentMiddleware[Any, Any, Any]] = [
            TodoListMiddleware(),
            # `_permissions` lets the filesystem tools filter deny'd paths out of ls/glob/grep
            # *results* -- the FilesystemPermissionsMiddleware below only guards the tool's path
            # argument, which a pathless grep/ls/glob would otherwise bypass entirely.
            FilesystemMiddleware(
                backend=backend,
                custom_tool_descriptions=_profile.tool_description_overrides,
                _permissions=permissions,
            ),
        ]
        if permissions:
            gp_middleware.append(FilesystemPermissionsMiddleware(permissions=permissions))
        gp_middleware.extend(
            [
                create_summarization_middleware(model, backend),
                PatchToolCallsMiddleware(),
            ]
        )
        if skills is not None:
            gp_middleware.append(SkillsMiddleware(backend=backend, sources=skills))
        # Core names captured before the tail so main-agent middleware that
        # overrides a GP slot replaces it in place (see `_gp_inheritable`).
        _gp_core_names = {m.name for m in gp_middleware}
        gp_middleware.extend(_profile.materialize_extra_middleware())
        if _profile.excluded_tools:
            gp_middleware.append(_ToolExclusionMiddleware(excluded=_profile.excluded_tools))
        _append_prompt_caching_middleware(gp_middleware, model)
        # Names of the GP slots (pre-exclusion) so we only inherit main-agent
        # middleware that overrides a default GP slot — not middleware specific
        # to the main agent.
        _gp_original_names = {m.name for m in gp_middleware}
        gp_middleware = _apply_excluded_middleware(
            gp_middleware,
            _profile,
            matched_classes=_excl_matched_classes,
            matched_names=_excl_matched_names,
        )
        _gp_inheritable = [m for m in (middleware or []) if m.name in _gp_original_names]
        gp_middleware = _apply_custom_middleware(gp_middleware, _gp_inheritable, core_names=_gp_core_names)
        gp_middleware = _apply_excluded_middleware(
            gp_middleware,
            _profile,
            matched_classes=_excl_matched_classes,
            matched_names=_excl_matched_names,
        )

        general_purpose_spec: SubAgent = {  # ty: ignore[missing-typed-dict-key]
            **GENERAL_PURPOSE_SUBAGENT,
            "model": model,
            "tools": _tools or [],
            "middleware": gp_middleware,
        }
        if gp_profile.description is not None:
            general_purpose_spec["description"] = gp_profile.description
        if gp_profile.system_prompt is not None:
            # A GP-specific override beats `profile.base_system_prompt`; only the
            # profile suffix still layers on top.
            gp_prompt = gp_profile.system_prompt
            if _profile.system_prompt_suffix is not None:
                gp_prompt = gp_prompt + "\n\n" + _profile.system_prompt_suffix
            general_purpose_spec["system_prompt"] = gp_prompt
        else:
            general_purpose_spec["system_prompt"] = _apply_profile_prompt(_profile, GENERAL_PURPOSE_SUBAGENT["system_prompt"])
        gp_interrupt_on = _merge_fs_interrupt_on(_build_interrupt_on_from_permissions(permissions or []), interrupt_on)
        if gp_interrupt_on is not None:
            general_purpose_spec["interrupt_on"] = gp_interrupt_on
        all_subagents = [general_purpose_spec, *processed_subagents]
        if f.enable_fork_subagent and not any(spec["name"] == "fork" for spec in processed_subagents):
            # ROADMAP #71: a fork shares the parent's base prompt, tools and
            # conversation, so its first call rides the parent's prefix.
            fork_spec: SubAgent = {  # ty: ignore[missing-typed-dict-key]
                **general_purpose_spec,
                "name": "fork",
                "description": FORK_SUBAGENT_DESCRIPTION,
                "mode": "fork",
                "system_prompt": _fork_system_prompt(system_prompt, _profile),
            }
            all_subagents.append(fork_spec)
    else:
        # GP stack not assembled: seed the coverage sets with this profile's
        # exclusions so an entry that would only have matched the (now-omitted)
        # GP stack doesn't spuriously trip the "matched nothing" audit below.
        for entry in _profile.excluded_middleware:
            if isinstance(entry, type):
                _excl_matched_classes.add(entry)
            else:
                _excl_matched_names.add(entry)
        all_subagents = list(processed_subagents)

    # P1-4 / S4: don't construct feature-wired middleware that the user has
    # already supplied via ``middleware=``. Two instances with the same
    # ``.name`` make langchain raise an opaque "Please remove duplicate
    # middleware instances" AssertionError that fingers the user's list, not
    # the injected twin. We resolve ``user_middleware`` up front so the
    # convenience-kwarg guards below (Skills/Memory/PromptCaching, in addition
    # to Filesystem/Summarization) can defer to the user's instance.
    user_middleware = list(middleware) if middleware else []
    user_supplied_skills = any(isinstance(m, SkillsMiddleware) for m in user_middleware)
    user_supplied_memory = any(isinstance(m, MemoryMiddleware) for m in user_middleware)
    user_supplied_prompt_caching = any(isinstance(m, AnthropicPromptCachingMiddleware) for m in user_middleware)

    # Build main agent middleware stack
    agents_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(),
    ]
    # Skills is appended before ``user_middleware`` (i.e. the convenience kwarg
    # wins position), so when the user also passes their own SkillsMiddleware we
    # skip the kwarg-built twin and honor the user's instance instead.
    if skills is not None and not user_supplied_skills:
        agents_middleware.append(SkillsMiddleware(backend=backend, sources=skills))
    # Enforce filesystem `deny` permissions as an early tool-call gate so denied
    # reads/writes never reach the backend. Interrupt-mode rules are wired into
    # HITL further down. Only installed when rules are supplied.
    if permissions:
        agents_middleware.append(FilesystemPermissionsMiddleware(permissions=permissions))

    # New feature middleware (#1-50)
    _wd = Path(f.working_dir) if f.working_dir else None

    # Deferred tool schemas: hide selected tool schemas from the model until
    # `tool_search` / `select` activate them, saving per-turn input tokens.
    # Placed at the head of the feature batch so its wrap runs before the
    # tool-injecting middleware below; the registry itself is populated from
    # the fully assembled request, so tools contributed by those middleware
    # (filesystem, subagent, …) are deferrable too.
    if f.enable_deferred_tools and (f.deferred_tools or f.deferred_keep_tools):
        from bog_agents.middleware.deferred_tools import DeferredToolsMiddleware

        agents_middleware.append(
            DeferredToolsMiddleware(
                deferred_names=frozenset(f.deferred_tools or ()),
                keep_names=frozenset(f.deferred_keep_tools or ()),
            )
        )

    if f.enable_git_tools:
        from bog_agents.middleware.git_tools import GitToolsMiddleware

        agents_middleware.append(GitToolsMiddleware(working_dir=_wd))

    if f.enable_repo_map:
        from bog_agents.middleware.repo_map import RepoMapMiddleware

        agents_middleware.append(RepoMapMiddleware(working_dir=_wd))

    if f.enable_checkpointing:
        from bog_agents.middleware.checkpointing import CheckpointingMiddleware

        agents_middleware.append(CheckpointingMiddleware(working_dir=_wd))

    if f.enable_evidence_bundle:
        # v6 SDK-11: the middleware and its tests existed but nothing could
        # reach it — no toggle, no CLI flag, no daemon dispatch.
        from bog_agents.middleware.evidence_bundle import EvidenceBundleMiddleware

        agents_middleware.append(
            EvidenceBundleMiddleware(
                working_dir=_wd,
                check_commands=[list(c) for c in (f.evidence_check_commands or [])] or None,
            )
        )

    if f.enable_cost_tracking or f.budget_usd is not None:
        from bog_agents._models import get_model_identifier
        from bog_agents.middleware.cost_tracker import CostTrackerMiddleware

        model_name = get_model_identifier(model) or "" if isinstance(model, BaseChatModel) else str(model or "")
        agents_middleware.append(CostTrackerMiddleware(model_name=model_name, budget_usd=f.budget_usd, effort_level=f.effort_level))

    if f.enable_plan_mode:
        from bog_agents.middleware.plan_mode import PlanModeMiddleware

        agents_middleware.append(PlanModeMiddleware(enabled=False))

    if f.architect_model or f.reviewer_model:
        from bog_agents.middleware.architect import ArchitectMiddleware

        agents_middleware.append(
            ArchitectMiddleware(
                architect_model=f.architect_model,
                reviewer_model=f.reviewer_model,
            )
        )

    if f.auto_lint or f.auto_test:
        from bog_agents.middleware.auto_quality import AutoQualityMiddleware

        agents_middleware.append(
            AutoQualityMiddleware(
                working_dir=_wd,
                auto_lint=f.auto_lint,
                auto_test=f.auto_test,
            )
        )

    # Feature middleware: worktree, multi-agent, context, etc.
    if f.enable_worktree:
        from bog_agents.middleware.worktree import WorktreeMiddleware

        agents_middleware.append(WorktreeMiddleware(working_dir=_wd))

    if f.enable_parallel_worktree:
        from bog_agents.middleware.worktree import ParallelWorktreeMiddleware

        agents_middleware.append(ParallelWorktreeMiddleware(working_dir=_wd))

    # NOTE: the enable_multi_agent flag intentionally wires nothing. The
    # MultiAgentOrchestratorMiddleware module was removed in the V1 stub purge
    # because it never actually ran agents; the flag is kept only so existing
    # callers passing it do not raise TypeError. It is now a deprecated no-op.
    # The old wiring here imported the deleted module and hard-crashed any
    # caller that enabled the flag. See REVIEW.md v2, finding P1-1.

    if f.enable_smart_context:
        from bog_agents.middleware.smart_context import SmartContextMiddleware

        agents_middleware.append(SmartContextMiddleware(working_dir=_wd, max_context_tokens=f.max_context_tokens))

    # Street sweeper — sits outer of the default SummarizationMiddleware (so it
    # trims litter before summarization measures the threshold) and inner of
    # CostTrackerMiddleware. It only edits message *content*, never count/order,
    # so summarization's cutoff indices and prompt-caching's prefix stay aligned.
    if f.enable_street_sweeper:
        from bog_agents._models import get_model_identifier
        from bog_agents.middleware.street_sweeper import StreetSweeperMiddleware

        sweeper_model = get_model_identifier(model) or "" if isinstance(model, BaseChatModel) else str(model or "")
        agents_middleware.append(
            StreetSweeperMiddleware(
                aggressive=f.street_sweeper_aggressive,
                keep_recent=f.street_sweeper_keep_recent,
                backend=backend,
                model_name=sweeper_model,
            )
        )

    if f.enable_conversation_branching:
        from bog_agents.middleware.conversation_branch import ConversationBranchMiddleware

        agents_middleware.append(ConversationBranchMiddleware(working_dir=_wd))

    if f.enable_image_input:
        from bog_agents.middleware.image_input import ImageInputMiddleware

        agents_middleware.append(ImageInputMiddleware(working_dir=_wd))

    if f.enable_browser:
        from bog_agents.middleware.browser_agent import BrowserAgentMiddleware

        agents_middleware.append(BrowserAgentMiddleware(working_dir=_wd, allowed_domains=f.allowed_browser_domains))

    if f.enable_pr_management:
        from bog_agents.middleware.pr_management import PRManagementMiddleware

        agents_middleware.append(PRManagementMiddleware(working_dir=_wd))

    if f.enable_test_tools:
        from bog_agents.middleware.test_generation import TestGenerationMiddleware

        agents_middleware.append(TestGenerationMiddleware(working_dir=_wd, test_framework=f.test_framework))

    if f.enable_enterprise:
        from bog_agents.middleware.enterprise import EnterpriseMiddleware

        agents_middleware.append(
            EnterpriseMiddleware(
                working_dir=_wd,
                team_config_path=Path(f.team_config_path) if f.team_config_path else None,
                current_role=f.current_role,
            )
        )

    if f.enable_multi_model:
        from bog_agents.middleware.multi_model import MultiModelMiddleware

        agents_middleware.append(MultiModelMiddleware(available_models=f.available_models))

    if f.enable_code_intelligence:
        from bog_agents.middleware.code_intelligence import CodeIntelligenceMiddleware

        agents_middleware.append(CodeIntelligenceMiddleware(working_dir=_wd))

    if f.enable_plugin_system:
        from bog_agents.middleware.plugin_system import PluginSystemMiddleware

        agents_middleware.append(PluginSystemMiddleware(plugins_dir=Path(f.plugins_dir) if f.plugins_dir else None))

    if f.enable_notifications:
        from bog_agents.middleware.notifications import NotificationsMiddleware

        agents_middleware.append(NotificationsMiddleware(session_name=f.session_name))

    # DLP must be appended BEFORE AuditTrail (V3-3): DLP redacts sensitive
    # values on the inbound path and must run *outer* (earlier) than Audit so
    # the audit log records the redacted request, not the raw secrets. (Only
    # the relative order matters; both are independently optional.)
    if f.enable_dlp:
        from bog_agents.middleware.dlp import DLPMiddleware

        agents_middleware.append(DLPMiddleware(mode=f.dlp_mode))

    # Financial advisor middleware
    if f.enable_audit_trail:
        from bog_agents.middleware.audit_trail import AuditTrailMiddleware

        agents_middleware.append(AuditTrailMiddleware(session_id=f.audit_session_id, advisor_id=f.audit_advisor_id))

    # ROADMAP #74: compliance artefact — hash-chained action log and OTLP export.
    if f.enable_action_log:
        import uuid as _uuid

        from bog_agents.action_log import ActionLog, ActionLogMiddleware

        _log_dir = Path(f.action_log_dir) if f.action_log_dir else Path.home() / ".bog-agents" / "action-log"
        _run_id = f.action_log_run_id or _uuid.uuid4().hex[:12]
        agents_middleware.append(ActionLogMiddleware(ActionLog(_log_dir / f"{_run_id}.jsonl", run_id=_run_id), price=_price_tokens))
    if f.otel_endpoint:
        from bog_agents.otel_export import OTelExportMiddleware, OTLPHttpSink

        agents_middleware.append(
            OTelExportMiddleware(OTLPHttpSink(f.otel_endpoint, headers=f.otel_headers), agent_name=name or "agent", price=_price_tokens)
        )

    # ROADMAP #72: governed code mode — bound to the assembled agent below.
    if f.enable_code_mode:
        from bog_agents.middleware.code_mode import CodeModeMiddleware

        agents_middleware.append(
            CodeModeMiddleware(
                timeout=f.code_mode_timeout,
                allowed_tools=f.code_mode_allowed_tools,
                cost_ledger=cost_ledger,
                max_calls=f.code_mode_max_calls,
            )
        )

    # D-5 provenance loop: composes citations + hallucination_detection
    # + fact_check so every claim the agent emits has a registered
    # source and the model is told to use the citation tools by default.
    # The umbrella flag ORs into each individual flag — callers can
    # still flip any of the three independently for granular control.
    provenance_active = f.enable_provenance_loop or f.enable_citations or f.enable_hallucination_detection or f.enable_fact_check
    if f.enable_provenance_loop or f.enable_citations:
        from bog_agents.middleware.citations import CitationsMiddleware

        agents_middleware.append(CitationsMiddleware())

    if f.enable_reasoning_chain:
        from bog_agents.middleware.reasoning_chain import ReasoningChainMiddleware

        agents_middleware.append(ReasoningChainMiddleware())

    if f.enable_provenance_loop or f.enable_hallucination_detection:
        from bog_agents.middleware.hallucination_detection import HallucinationDetectionMiddleware

        agents_middleware.append(HallucinationDetectionMiddleware())

    if f.enable_enhanced_skills:
        if not f.enhanced_skills_sources:
            logging.getLogger(__name__).warning(
                "enable_enhanced_skills=True but f.enhanced_skills_sources is empty — "
                "EnhancedSkillsMiddleware will not be activated. "
                "Pass enhanced_skills_sources=['/path/to/skills'] to enable."
            )
        else:
            from bog_agents.middleware.enhanced_skills import EnhancedSkillsMiddleware

            agents_middleware.append(
                EnhancedSkillsMiddleware(
                    backend=backend,
                    sources=f.enhanced_skills_sources,
                    cache_dir=f.enhanced_skills_cache_dir,
                )
            )

    if f.enable_saved_prompts and f.saved_prompts_sources:
        from bog_agents.middleware.saved_prompts import SavedPromptsMiddleware

        agents_middleware.append(SavedPromptsMiddleware(backend=backend, sources=f.saved_prompts_sources))

    # Vertical-market batch (deep_research, dlp, version_control, nl_query, …)
    # — Wave V removed the financial-advisor stubs that lived here.
    if f.enable_deep_research:
        from bog_agents.middleware.deep_research import DeepResearchMiddleware

        agents_middleware.append(DeepResearchMiddleware())

    # NOTE: DLPMiddleware is appended earlier (before AuditTrail) — see V3-3.

    if f.enable_version_control:
        from bog_agents.middleware.version_control import VersionControlMiddleware

        agents_middleware.append(VersionControlMiddleware())

    if f.enable_nl_query:
        from bog_agents.middleware.nl_query import NLQueryMiddleware

        agents_middleware.append(NLQueryMiddleware())

    if f.enable_code_review:
        from bog_agents.middleware.code_review import CodeReviewMiddleware

        agents_middleware.append(CodeReviewMiddleware())

    if f.enable_model_portfolio:
        from bog_agents.middleware.model_portfolio import ModelPortfolioMiddleware

        agents_middleware.append(ModelPortfolioMiddleware())

    if f.enable_knowledge_graph:
        from bog_agents.middleware.knowledge_graph import KnowledgeGraphMiddleware

        agents_middleware.append(KnowledgeGraphMiddleware())

    if f.enable_rbac:
        from bog_agents.middleware.rbac import RBACMiddleware

        # MW-SAFE-2: pass operator-owned roles/active_role so the model can't
        # self-administer the policy. Without a pinned role RBAC has nothing to
        # enforce (no boundary against an adversarial model) — warn so the
        # operator isn't given false assurance.
        # SDKC-1: use the module-level `logging` import — this line used to read
        # `_logging.getLogger(...)`, a function-local name only bound inside the
        # enhanced-skills branch, so enable_rbac without a role crashed with
        # UnboundLocalError before the graph was built.
        if not f.rbac_active_role:
            logging.getLogger(__name__).warning(
                "enable_rbac=True without rbac_active_role: RBAC exposes role tools to the model but enforces no restriction until a role is pinned."
            )
        agents_middleware.append(RBACMiddleware(roles=f.rbac_roles, active_role=f.rbac_active_role))

    if f.enable_provenance_loop or f.enable_fact_check:
        from bog_agents.middleware.fact_check import FactCheckMiddleware

        agents_middleware.append(FactCheckMiddleware())

    if f.enable_approval_gates:
        from bog_agents.middleware.approval_gates import ApprovalGatesMiddleware

        agents_middleware.append(ApprovalGatesMiddleware())

    # Browser + remote-execution batch
    if f.enable_browser_agent_fa:
        from bog_agents.middleware.browser_agent_fa import BrowserAgentFAMiddleware

        agents_middleware.append(BrowserAgentFAMiddleware())

    if f.enable_automations:
        from bog_agents.middleware.automations import AutomationsMiddleware

        agents_middleware.append(AutomationsMiddleware())

    if f.enable_image_pdf_input:
        from bog_agents.middleware.image_pdf_input import ImagePdfInputMiddleware

        agents_middleware.append(ImagePdfInputMiddleware())

    if f.enable_cloud_sandbox:
        from bog_agents.middleware.cloud_sandbox import CloudSandboxMiddleware

        agents_middleware.append(CloudSandboxMiddleware())

    if f.enable_computer_use:
        from bog_agents.middleware.computer_use import ComputerUseMiddleware

        agents_middleware.append(ComputerUseMiddleware())

    if f.enable_opensearch_rag:
        from bog_agents.middleware.opensearch_rag import OpenSearchRAGMiddleware

        agents_middleware.append(OpenSearchRAGMiddleware())

    if f.enable_air_gapped:
        from bog_agents.middleware.air_gapped import AirGappedMiddleware, DataPolicy

        # MW-SAFE-1: the flag path always pins a policy (operator-supplied, or a
        # default fail-closed one) so egress is operator-owned and the model
        # cannot lift it via set_data_policy/clear_air_gap.
        air_gap_policy = f.air_gap_policy if f.air_gap_policy is not None else DataPolicy()
        agents_middleware.append(AirGappedMiddleware(policy=air_gap_policy))

    if f.enable_dashboard:
        from bog_agents.middleware.dashboard import DashboardMiddleware

        agents_middleware.append(DashboardMiddleware())

    if f.enable_scheduled_reports:
        from bog_agents.middleware.scheduled_reports import ScheduledReportsMiddleware

        agents_middleware.append(ScheduledReportsMiddleware())

    if f.enable_collaborative_sessions:
        from bog_agents.middleware.collaborative_sessions import CollaborativeSessionsMiddleware

        agents_middleware.append(CollaborativeSessionsMiddleware())

    if f.enable_messaging_integration:
        from bog_agents.middleware.messaging_integration import MessagingIntegrationMiddleware

        agents_middleware.append(MessagingIntegrationMiddleware())

    if f.enable_voice_io:
        from bog_agents.middleware.voice_io import VoiceIOMiddleware

        agents_middleware.append(VoiceIOMiddleware())

    if f.enable_competitive_intel:
        from bog_agents.middleware.competitive_intel import CompetitiveIntelMiddleware

        agents_middleware.append(CompetitiveIntelMiddleware())

    if f.enable_result_synthesis:
        from bog_agents.middleware.result_synthesis import ResultSynthesisMiddleware
        from bog_agents.middleware.worktree import ParallelWorktreeMiddleware

        # ResultSynthesisMiddleware requires a ParallelWorktreeMiddleware that
        # appears earlier in the stack (enforced by `requires=`). Search BOTH
        # the feature-wired stack built so far AND the not-yet-appended
        # ``user_middleware`` (V3-24): if we only searched ``agents_middleware``,
        # a user passing their own ParallelWorktreeMiddleware via ``middleware=``
        # would get a second auto-provisioned instance, and the keep-first dedup
        # pass would then silently discard the user's configured instance in
        # favor of our default. Prefer the user's instance and reposition it
        # before ResultSynthesis so ordering holds without auto-provisioning a
        # twin. The later copy left in ``user_middleware`` is dropped by the
        # keep-first ``_dedup_middleware_by_name`` backstop.
        parallel_mw = next((m for m in agents_middleware if isinstance(m, ParallelWorktreeMiddleware)), None)
        if parallel_mw is None:
            parallel_mw = next((m for m in user_middleware if isinstance(m, ParallelWorktreeMiddleware)), None)
            if parallel_mw is not None:
                agents_middleware.append(parallel_mw)
        if parallel_mw is None:
            # Neither the feature stack nor the user supplied one; auto-create
            # one rather than crashing at validation time.
            parallel_mw = ParallelWorktreeMiddleware(working_dir=_wd)
            agents_middleware.append(parallel_mw)
        agents_middleware.append(ResultSynthesisMiddleware(parallel_middleware=parallel_mw))

    # P1-4: don't double-append defaults the user has already supplied
    # via ``middleware=``. The previous behavior appended both, which
    # ran the same middleware twice per request. We check by class
    # identity (covers subclasses too) so a user who has subclassed
    # FilesystemMiddleware for custom behavior takes precedence.
    # (``user_middleware`` was resolved up front, near the Skills guard.)
    user_supplied_filesystem = any(isinstance(m, FilesystemMiddleware) for m in user_middleware)
    user_supplied_summarization = any(isinstance(m, _BogAgentsSummarizationMiddleware) for m in user_middleware)

    defaults_to_append: list[Any] = []
    if not user_supplied_filesystem:
        defaults_to_append.append(
            FilesystemMiddleware(
                backend=backend,
                custom_tool_descriptions=_profile.tool_description_overrides,
                _permissions=permissions,
            )
        )
    # Only install SubAgentMiddleware (the `task` tool backend) when there is at
    # least one synchronous subagent to dispatch to. With the general-purpose
    # subagent disabled via `GeneralPurposeSubagentProfile(enabled=False)` and no
    # user-supplied synchronous subagents, `all_subagents` is empty and the
    # `task` tool is dropped. Async subagents are independent.
    if all_subagents:
        defaults_to_append.append(
            SubAgentMiddleware(
                backend=backend,
                subagents=all_subagents,
                # Overrides the task tool description. Value should include the
                # `{available_agents}` placeholder; `None` (default) uses the
                # built-in template.
                task_description=_profile.tool_description_overrides.get("task"),
                cost_ledger=cost_ledger,
            )
        )
    if not user_supplied_summarization:
        defaults_to_append.append(create_summarization_middleware(model, backend))
    defaults_to_append.append(PatchToolCallsMiddleware())
    # Auto-continue responses truncated at the output-token limit. Placed
    # inside PatchToolCalls / Summarization so a continuation re-invokes only
    # the raw model call (never re-running compaction or argument truncation).
    # The tail wrappers (Memory injection, PromptCaching) still wrap it, which
    # is harmless: the continuation presents a fresh message list.
    defaults_to_append.append(OutputTruncationMiddleware())

    agents_middleware.extend(defaults_to_append)
    if async_subagents:
        agents_middleware.append(AsyncSubAgentMiddleware(async_subagents=async_subagents, cost_ledger=cost_ledger))

    # User-supplied middleware: a `.name` collision with a built-in REPLACES it
    # in place (parity with upstream); novel middleware splices in after the
    # core stack, ahead of the profile/prompt-caching/memory tail.
    _main_core_names = {m.name for m in agents_middleware}
    agents_middleware = _apply_custom_middleware(agents_middleware, user_middleware, core_names=_main_core_names)
    # Harness-profile `extra_middleware` (no-op unless a profile is registered
    # for this model). Placed after user middleware and before prompt caching so
    # caller middleware retains precedence.
    agents_middleware.extend(_profile.materialize_extra_middleware())
    # Strip harness-profile excluded tools after every tool-injecting middleware
    # has run, so it can remove both user-supplied and middleware-injected tools.
    if _profile.excluded_tools:
        agents_middleware.append(_ToolExclusionMiddleware(excluded=_profile.excluded_tools))
    # Guardrail tripwires (#18): validate the inbound user message and the model
    # response, failing fast on a violation. Applied to both stages from the flat
    # `guardrails=` list; for asymmetric input/output control pass a configured
    # GuardrailMiddleware via `middleware=` instead. Placed near the model so it
    # sees the assembled request and the raw response.
    if guardrails:
        from bog_agents.guardrails import GuardrailMiddleware

        agents_middleware.append(GuardrailMiddleware(input_guardrails=list(guardrails), output_guardrails=list(guardrails)))

    # Memory must be appended BEFORE AnthropicPromptCachingMiddleware (V3-2):
    # Memory.modify_request appends a new system content block; PromptCaching
    # tags the *last* system block with cache_control. If Memory ran after
    # caching, the injected memory text would fall outside the cached prefix
    # (and per-thread memory variance would bust cache hits). Keeping Memory
    # outer (earlier) and PromptCaching innermost preserves the cached prefix.
    # S4: skip the convenience-kwarg twin when the user already passed their
    # own MemoryMiddleware / AnthropicPromptCachingMiddleware via ``middleware=``
    # (those instances are already in ``agents_middleware`` at this point).
    if memory is not None and not user_supplied_memory:
        agents_middleware.append(MemoryMiddleware(backend=backend, sources=memory))
    if not user_supplied_prompt_caching:
        _append_prompt_caching_middleware(agents_middleware, model)
    main_interrupt_on = _merge_fs_interrupt_on(_build_interrupt_on_from_permissions(permissions or []), interrupt_on)
    if main_interrupt_on is not None:
        agents_middleware.append(HumanInTheLoopMiddleware(interrupt_on=main_interrupt_on))
    # ROADMAP #52: the cache-bust detector must be the innermost model-call
    # observer so the prefix it fingerprints is exactly what the provider sees
    # (after summarization, memory, caching tags — every other injector).
    if f.enable_cache_diagnostics:
        from bog_agents.middleware.cache_diagnostics import CacheBustDetectorMiddleware

        agents_middleware.append(CacheBustDetectorMiddleware(events_dir=f.cache_diagnostics_dir))

    # Filter `HarnessProfile.excluded_middleware` from the fully assembled main
    # stack, then verify every exclusion matched something across all stacks
    # (main + GP + subagents). No-op when no profile is registered.
    agents_middleware = _apply_excluded_middleware(
        agents_middleware,
        _profile,
        matched_classes=_excl_matched_classes,
        matched_names=_excl_matched_names,
    )
    _verify_excluded_middleware_coverage(
        _profile,
        _excl_matched_classes,
        _excl_matched_names,
        required_classes=_required_mw_classes,
        required_names=_required_mw_names,
    )

    # Backstop dedup: drop any duplicate-name middleware the per-feature guards
    # above didn't catch, so langchain's "Please remove duplicate middleware
    # instances" assertion can never surprise the caller. Keep-first is safe
    # here because the framework-injected twins are already suppressed at
    # construction time, so the user's instance is the one that survives.
    agents_middleware = _dedup_middleware_by_name(agents_middleware)

    # Validate middleware dependency ordering before compiling the graph.
    _validate_middleware_ordering(agents_middleware)

    # Assemble the main-agent prompt from ordered parts:
    #   prefix -> base -> suffix -> profile suffix -> provenance addendum
    #
    # `prefix`/`suffix` come from a `SystemPromptConfig` (a bare `str` /
    # `SystemMessage` normalizes to `prefix`, preserving the historical
    # "caller text before the base" behavior). The config's `base` key
    # overrides the profile base when present; `base: None` (present but
    # `None`) drops the base entirely, while an OMITTED key keeps the
    # profile's `base_system_prompt` (or `BASE_AGENT_PROMPT`).
    #
    # The provenance-loop addendum (D-5) is appended as ITS OWN part rather
    # than folded into the base, so `base: None` never silently un-prompts
    # the bound citation / hallucination-detection / fact-check tools. It is
    # only injected when that loop is active — otherwise the model would be
    # told to call tools that aren't bound.
    cfg = _normalize_system_prompt(system_prompt)
    prompt_parts: list[str | SystemMessage] = []
    prefix = cfg.get("prefix")
    if prefix is not None:
        prompt_parts.append(prefix)
    profile_base = _profile.base_system_prompt if _profile.base_system_prompt is not None else BASE_AGENT_PROMPT
    # Two-arg `.get` so a present-but-`None` `base` key drops the base, while
    # an omitted key falls back to the profile/default base.
    base = cfg.get("base", profile_base)
    if base is not None:
        prompt_parts.append(base)
    suffix = cfg.get("suffix")
    if suffix is not None:
        prompt_parts.append(suffix)
    if _profile.system_prompt_suffix is not None:
        prompt_parts.append(_profile.system_prompt_suffix)
    if provenance_active:
        prompt_parts.append(_PROVENANCE_LOOP_PROMPT)
    final_system_prompt: str | SystemMessage = _assemble_prompt_parts(prompt_parts)

    # Only forward `state_schema` when the caller opted in, so the default
    # behavior (LangChain's built-in AgentState) is preserved unchanged.
    _create_kwargs: dict[str, Any] = {}
    if state_schema is not None:
        _create_kwargs["state_schema"] = state_schema

    # ROADMAP #54: let a running token audit see (and instrument) the final
    # stack before LangChain binds the hooks. No-op outside `capture_assembly`.
    for _code_mode in agents_middleware:
        if type(_code_mode).__name__ == "CodeModeMiddleware":
            _code_mode.bind(_tools, agents_middleware)  # ROADMAP #72: learn tools + governance chain
    notify_assembly(agents_middleware, _tools, final_system_prompt)

    return _langchain_create_agent(
        model,
        system_prompt=final_system_prompt,
        tools=_tools,
        middleware=agents_middleware,
        response_format=response_format,
        context_schema=context_schema,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
        name=name,
        cache=cache,
        **_create_kwargs,
    ).with_config(
        {
            # Honor `max_turns` without the old hard clamp at 1000 (parity with
            # upstream, which caps at 9,999); the floor of 10 keeps a usable
            # minimum. Bog's native default (`max_turns=200`) is unchanged.
            "recursion_limit": max(10, max_turns),
            "metadata": {
                "ls_integration": "bog-agents",
                # Langsmith metadata key is `lc_versions` (parity with upstream);
                # the prior `versions` key was a bog-local divergence.
                "lc_versions": {"bog-agents": __version__},
                "lc_agent_name": name,
            },
        }
    )
