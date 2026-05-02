"""Shared helpers for resolving and inspecting chat models."""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


# Long-output, long-thinking model turns can run for many minutes, especially
# for /review-style tasks that hit deep reasoning and large outputs. The default
# is intentionally generous so legitimate long turns never get cut off; users
# who want a shorter ceiling can lower it via the env var.
_DEFAULT_MODEL_READ_TIMEOUT_SECS: float = 3600.0


def _resolve_model_read_timeout() -> float | None:
    """Resolve the per-model HTTP read timeout from env or default.

    Returns:
        Read timeout in seconds, or `None` to disable the read deadline.
    """
    raw = os.environ.get("BOG_AGENTS_MODEL_READ_TIMEOUT")
    if raw is None:
        return _DEFAULT_MODEL_READ_TIMEOUT_SECS
    if raw.strip().lower() in {"none", "off", "0", ""}:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid BOG_AGENTS_MODEL_READ_TIMEOUT=%r, using default %ss",
            raw,
            _DEFAULT_MODEL_READ_TIMEOUT_SECS,
        )
        return _DEFAULT_MODEL_READ_TIMEOUT_SECS
    if value <= 0:
        return None
    return value


def _bedrock_config_kwarg(timeout_secs: float | None) -> dict[str, Any]:
    """Build a `config=` kwarg for a Bedrock-flavored chat model.

    Bedrock providers don't accept a flat `timeout=` kwarg the way Anthropic /
    OpenAI / Gemini do. Their HTTP layer is botocore, which takes `read_timeout`
    and `connect_timeout` via a `botocore.config.Config` object passed as
    `config=`. Returns an empty dict when timeout is disabled or botocore is
    not importable, so the caller can splat unconditionally.

    Args:
        timeout_secs: Read timeout in seconds, or `None` to disable.

    Returns:
        Dict with a single `config` key when applicable, else `{}`.
    """
    if timeout_secs is None:
        return {}
    try:
        from botocore.config import Config
    except ImportError:
        return {}
    return {
        "config": Config(
            read_timeout=int(timeout_secs),
            connect_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
        )
    }


def _model_init_kwargs(model: str, timeout_secs: float | None) -> dict[str, Any]:
    """Compute provider-aware kwargs for `init_chat_model` for a given spec.

    Args:
        model: Model spec like `provider:identifier`.
        timeout_secs: Read timeout in seconds, or `None` to skip.

    Returns:
        Dict of kwargs to splat into `init_chat_model`.
    """
    kwargs: dict[str, Any] = {}
    if model.startswith("openai:"):
        kwargs["use_responses_api"] = True
    if timeout_secs is None:
        return kwargs
    if model.startswith(("bedrock:", "bedrock_converse:")):
        kwargs.update(_bedrock_config_kwarg(timeout_secs))
    else:
        # Anthropic, OpenAI, Gemini, Mistral, Cohere, Groq, DeepSeek, etc. all
        # accept a flat `timeout=` kwarg that flows down to their HTTP client.
        kwargs["timeout"] = timeout_secs
    return kwargs


def resolve_model(model: str | BaseChatModel) -> BaseChatModel:
    """Resolve a model string to a `BaseChatModel`.

    If `model` is already a `BaseChatModel`, returns it unchanged. String
    models are resolved via `init_chat_model` with provider-aware timeout
    kwargs so long-running model turns don't get cut off by the provider
    SDK's stock 60-300s read deadline. Override the timeout with
    `BOG_AGENTS_MODEL_READ_TIMEOUT` (seconds, or `none`/`0` to disable).

    OpenAI models (prefixed with `openai:`) default to the Responses API.

    Args:
        model: Model string or pre-configured model instance.

    Returns:
        Resolved `BaseChatModel` instance.
    """
    if isinstance(model, BaseChatModel):
        return model
    timeout_secs = _resolve_model_read_timeout()
    kwargs = _model_init_kwargs(model, timeout_secs)
    try:
        return init_chat_model(model, **kwargs)
    except TypeError as exc:
        # Provider rejected `timeout` (or `config`). Retry without those so
        # users on exotic providers aren't blocked by an unsupported kwarg.
        if "timeout" not in kwargs and "config" not in kwargs:
            raise
        logger.warning(
            "Provider for %r rejected timeout kwargs (%s); retrying without",
            model,
            exc,
        )
        kwargs.pop("timeout", None)
        kwargs.pop("config", None)
        return init_chat_model(model, **kwargs)


def get_model_identifier(model: BaseChatModel) -> str | None:
    """Extract the provider-native model identifier from a chat model.

    Providers do not agree on a single field name for the identifier. Some use
    `model_name`, while others use `model`. Reading the serialized model config
    lets us inspect both without relying on reflective attribute access.

    Args:
        model: Chat model instance to inspect.

    Returns:
        The configured model identifier, or `None` if it is unavailable.
    """
    config = model.model_dump()
    return _string_value(config, "model_name") or _string_value(config, "model")


def model_matches_spec(model: BaseChatModel, spec: str) -> bool:
    """Check whether a model instance already matches a string model spec.

    Matching is performed in two ways: first by exact string equality between
    `spec` and the model identifier, then by comparing only the model-name
    portion of a `provider:model` spec against the identifier. For example,
    `"openai:gpt-5"` matches a model with identifier `"gpt-5"`.

    Assumes the `provider:model` convention (single colon separator).

    Args:
        model: Chat model instance to inspect.
        spec: Model spec in `provider:model` format (e.g., `openai:gpt-5`).

    Returns:
        `True` if the model already matches the spec, otherwise `False`.
    """
    current = get_model_identifier(model)
    if current is None:
        return False
    if spec == current:
        return True

    _, separator, model_name = spec.partition(":")
    return bool(separator) and model_name == current


def _string_value(config: dict[str, Any], key: str) -> str | None:
    """Return a non-empty string value from a serialized model config."""
    value = config.get(key)
    if isinstance(value, str) and value:
        return value
    return None
