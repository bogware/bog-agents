"""CLI middleware for runtime model selection via LangGraph runtime context.

Allows switching the model per invocation by passing a `CLIContext` via
`context=` on `agent.astream()` / `agent.invoke()` without recompiling
the graph.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bog_agents._models import model_matches_spec, resolve_model  # noqa: PLC2701
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ContentBlock, SystemMessage
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


logger = logging.getLogger(__name__)


class CLIContext(TypedDict, total=False):
    """Runtime context passed via `context=` to the LangGraph graph.

    Carries per-invocation overrides that `ConfigurableModelMiddleware`
    reads from `request.runtime.context`.
    """

    model: str | None
    """Model spec to swap at runtime (e.g. `'openai:gpt-4o'`)."""

    model_params: dict[str, Any]
    """Invocation params (e.g. `temperature`, `max_tokens`) to merge
    into `model_settings`."""

    effort_level: str | None
    """Runtime reasoning preset (`low`, `medium`, `high`, or `max`)."""

    plan_mode: bool
    """Whether read-only plan mode should be enforced for this turn."""

    system_prompt_append: str | None
    """Additional system guidance appended to the request's system prompt."""


def _is_anthropic_model(model: object) -> bool:
    """Check whether a resolved model is an Anthropic `ChatAnthropic` instance.

    Returns `False` if `langchain-anthropic` is not installed.

    Returns:
        `True` if the model is a `ChatAnthropic` instance.
    """
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        logger.debug("langchain_anthropic not installed; assuming non-Anthropic model")
        return False
    return isinstance(model, ChatAnthropic)


def _is_ollama_model(model: object) -> bool:
    """Check whether a resolved model is an Ollama `ChatOllama` instance.

    Returns `False` if `langchain-ollama` is not installed.

    Returns:
        `True` if the model is a `ChatOllama` instance.
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        logger.debug("langchain_ollama not installed; assuming non-Ollama model")
        return False
    return isinstance(model, ChatOllama)


_ANTHROPIC_ONLY_SETTINGS: set[str] = {"cache_control"}
"""Keys injected by Anthropic-specific middleware (e.g.
`AnthropicPromptCachingMiddleware`) that are not accepted by other providers and
must be stripped on cross-provider swap."""

_EFFORT_LEVEL_SETTINGS: dict[str, dict[str, Any]] = {
    "low": {"max_tokens": 1024, "temperature": 0.3},
    "medium": {"max_tokens": 4096, "temperature": 0.5},
    "high": {"max_tokens": 8192, "temperature": 0.7},
    "max": {"max_tokens": 16384, "temperature": 1.0},
}
"""Best-effort runtime model settings for effort presets."""

_OLLAMA_SETTING_ALIASES: dict[str, str] = {
    "max_tokens": "num_predict",
}
"""Runtime setting aliases required by Ollama-compatible model calls."""

_PLAN_MODE_MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "multi_edit_file",
        "execute",
        "git_commit",
        "git_add",
        "git_stash",
    }
)
"""Tool names hidden from the model when runtime plan mode is enabled."""

_PLAN_MODE_PROMPT = """
## Plan Mode Active

You are currently in plan mode for this turn.
- Read, inspect, search, and reason about the codebase
- Do not write files, edit code, or execute mutating actions
- Produce a concrete plan the user can approve or refine
"""
"""Runtime system guidance injected when plan mode is enabled."""


def _append_system_message(
    system_message: SystemMessage | None,
    text: str,
) -> SystemMessage:
    """Append text to an existing system message or create a new one."""
    new_content: list[ContentBlock] = (
        list(system_message.content_blocks) if system_message else []
    )
    if new_content:
        text = f"\n\n{text}"
    new_content.append({"type": "text", "text": text})
    return SystemMessage(content_blocks=new_content)


def _normalize_model_settings(
    model: object,
    model_settings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize provider-specific `model_settings` before invocation."""
    if model_settings is None:
        return None

    normalized = dict(model_settings)

    if _is_ollama_model(model):
        for source_key, target_key in _OLLAMA_SETTING_ALIASES.items():
            if source_key not in normalized:
                continue
            if target_key not in normalized:
                normalized[target_key] = normalized[source_key]
            normalized.pop(source_key, None)

    return normalized


def _apply_ollama_runtime_model_updates(
    model: object,
    model_settings: dict[str, Any] | None,
) -> tuple[object, dict[str, Any] | None]:
    """Apply Ollama generation settings onto the model instance itself.

    `langchain-ollama` expects controls like `temperature` and `num_predict`
    on the `ChatOllama` model rather than as per-call invocation kwargs.
    """
    if model_settings is None or not _is_ollama_model(model):
        return model, model_settings

    field_names = set(getattr(type(model), "model_fields", {}))
    updates = {
        key: value for key, value in model_settings.items() if key in field_names
    }
    if not updates:
        return model, model_settings

    cloned_model = model.model_copy(update=updates)  # ty: ignore[unresolved-attribute]
    remaining_settings = {k: v for k, v in model_settings.items() if k not in updates}
    return cloned_model, remaining_settings


def _apply_overrides(request: ModelRequest) -> ModelRequest:
    """Apply model/param overrides from `CLIContext` on the runtime.

    Returns:
        The original request unchanged when no `CLIContext` is present or it
            contains no overrides, otherwise a new request with overrides.
    """
    runtime = request.runtime
    if runtime is None:
        return request

    ctx = runtime.context
    if not isinstance(ctx, dict):
        return request

    overrides: dict[str, Any] = {}

    # Model swap
    new_model = None
    model = ctx.get("model")
    if model and not model_matches_spec(request.model, model):
        logger.debug("Overriding model to %s", model)
        new_model = resolve_model(model)
        overrides["model"] = new_model

    # Param merge
    model_params = ctx.get("model_params", {})
    effort_level = ctx.get("effort_level")
    effort_settings = (
        _EFFORT_LEVEL_SETTINGS.get(effort_level, {})
        if isinstance(effort_level, str)
        else {}
    )
    base_model_settings = request.model_settings or {}
    merged_model_settings = {
        **base_model_settings,
        **effort_settings,
        **model_params,
    }
    if merged_model_settings != base_model_settings:
        overrides["model_settings"] = merged_model_settings

    if overrides:
        request = request.override(**overrides)

    # When switching away from Anthropic, strip provider-specific settings
    # that would cause errors on other providers (e.g. cache_control passed
    # to the OpenAI SDK raises TypeError).
    if new_model is not None and not _is_anthropic_model(new_model):
        settings = request.model_settings or {}
        dropped = settings.keys() & _ANTHROPIC_ONLY_SETTINGS
        if dropped:
            logger.debug(
                "Stripped Anthropic-only settings %s for non-Anthropic model",
                dropped,
            )
            request = request.override(
                model_settings={k: v for k, v in settings.items() if k not in dropped}
            )

    normalized_settings = _normalize_model_settings(
        request.model, request.model_settings
    )
    runtime_model, normalized_settings = _apply_ollama_runtime_model_updates(
        request.model,
        normalized_settings,
    )
    if (
        runtime_model is not request.model
        or normalized_settings != request.model_settings
    ):
        request = request.override(
            model=runtime_model,
            model_settings=normalized_settings,
        )

    system_prompt_append = ctx.get("system_prompt_append")
    plan_mode = bool(ctx.get("plan_mode"))
    prompt_parts = []
    if system_prompt_append:
        prompt_parts.append(system_prompt_append)
    if plan_mode:
        prompt_parts.append(_PLAN_MODE_PROMPT.strip())
    if prompt_parts:
        request = request.override(
            system_message=_append_system_message(
                request.system_message,
                "\n\n".join(prompt_parts),
            )
        )

    if plan_mode:
        request = request.override(
            tools=[
                tool
                for tool in request.tools
                if getattr(tool, "name", "") not in _PLAN_MODE_MUTATING_TOOLS
            ]
        )

    return request


class ConfigurableModelMiddleware(AgentMiddleware):
    """Swap the model or per-call settings from `runtime.context`."""

    def wrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Apply runtime overrides and delegate to the next handler."""
        return handler(_apply_overrides(request))

    async def awrap_model_call(  # noqa: PLR6301
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Apply runtime overrides and delegate to the next async handler."""
        return await handler(_apply_overrides(request))
