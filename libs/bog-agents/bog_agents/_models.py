"""Shared helpers for resolving and inspecting chat models."""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


# Long-output, long-thinking model turns can legitimately run for an hour
# or more, especially for /review-style tasks chained with long tool calls
# (builds, test suites, fan-out research) inside a single turn. The default
# is deliberately generous so legitimate long work never gets cut off; users
# who want a tighter ceiling can lower it via the env var, and setting it
# to ``none``/``0`` disables the read deadline entirely.
_DEFAULT_MODEL_READ_TIMEOUT_SECS: float = 7200.0


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


# ---------------------------------------------------------------------------
# Bedrock inference-profile auto-resolver
# ---------------------------------------------------------------------------
#
# Anthropic Claude 4.x on AWS Bedrock requires a cross-region inference
# profile prefix (`us.`, `eu.`, `apac.`, ...). The bare model id
# `anthropic.claude-opus-4-7` returns `AccessDeniedException` even when
# the account has model access granted in the console — AWS treats the
# bare id as a request for on-demand throughput, which has been deprecated
# for the Claude 4.x line.
#
# The auto-resolver below rewrites a bare `anthropic.*` id to the correct
# regional prefix based on `AWS_REGION` / `AWS_DEFAULT_REGION` so users
# who paste the platform.claude.com name into their config still land
# on a working model. A warning is logged so the rewrite is visible in
# `--debug` output and the user can switch to the explicit form.

_BEDROCK_REGION_PROFILE_MAP: dict[str, str] = {
    # US / Canada → us. profiles (work from us-east-1, us-east-2, us-west-2,
    # ca-central-1).
    "us": "us",
    "ca": "us",
    # Europe → eu. profiles (eu-west-1, eu-west-3, eu-central-1, eu-north-1).
    "eu": "eu",
    # Asia-Pacific → apac., with Japan as a separate `jp.` family.
    "ap-northeast-1": "jp",  # Tokyo
    "ap": "apac",
    # South America → sa. (sa-east-1).
    "sa": "sa",
}


def _resolve_bedrock_region_prefix(region: str | None) -> str:
    """Map an AWS region to the matching inference-profile prefix.

    Falls back to ``us`` when the region is missing or unknown — `us.` profiles
    are available in the most regions and are the safest default.

    Args:
        region: AWS region string like ``us-east-1`` or ``None``.

    Returns:
        The profile prefix without the trailing dot (e.g. ``"us"``).
    """
    if not region:
        return "us"
    region = region.lower().strip()
    # Try most specific first (full region match).
    if region in _BEDROCK_REGION_PROFILE_MAP:
        return _BEDROCK_REGION_PROFILE_MAP[region]
    # Then the geo prefix (first segment before the first dash).
    geo = region.split("-", 1)[0]
    return _BEDROCK_REGION_PROFILE_MAP.get(geo, "us")


_BEDROCK_PROFILE_PREFIXES: tuple[str, ...] = (
    "us.",
    "eu.",
    "apac.",
    "jp.",
    "sa.",
    "global.",
)
"""Recognised cross-region inference profile prefixes."""


def _normalize_bedrock_model_id(model: str) -> str:
    """Rewrite a bare Anthropic Claude Bedrock id to a regional profile id.

    Only applies when:
    1. The spec uses the ``bedrock:`` or ``bedrock_converse:`` provider.
    2. The model id starts with ``anthropic.`` (bare, no regional prefix).
    3. The model id matches Claude 4.x / 4.5+ (the family that requires
       inference profiles on Bedrock).

    All other inputs are returned unchanged. Logs the rewrite at WARNING
    level so it's visible in `--debug` output.

    Args:
        model: Full spec like ``bedrock_converse:anthropic.claude-opus-4-7``.

    Returns:
        Possibly-rewritten spec.
    """
    if not model.startswith(("bedrock:", "bedrock_converse:")):
        return model
    provider, _, model_id = model.partition(":")
    if not model_id:
        return model
    # Already has a regional / global profile prefix — nothing to do.
    if model_id.lower().startswith(_BEDROCK_PROFILE_PREFIXES):
        return model
    # Only rewrite Anthropic Claude 4.x / 4.5+ family — those are the
    # ones AWS gates behind inference profiles. Nova/Llama/Mistral
    # bare ids still work for on-demand throughput.
    if not model_id.lower().startswith("anthropic.claude-"):
        return model
    # Coarse Claude-4-or-newer check: matches `claude-opus-4-*`,
    # `claude-sonnet-4-*`, `claude-haiku-4-*`, `claude-opus-5-*` (future).
    # Older `claude-3-*` IDs still work bare on Bedrock and are left alone.
    lowered = model_id.lower()
    is_modern = any(f"claude-{variant}-{major}" in lowered for variant in ("opus", "sonnet", "haiku") for major in ("4", "5", "6", "7", "8", "9"))
    if not is_modern:
        return model
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    prefix = _resolve_bedrock_region_prefix(region)
    rewritten = f"{provider}:{prefix}.{model_id}"
    logger.warning(
        "Bedrock: rewrote bare Claude id %r to %r (AWS_REGION=%s). "
        "Claude 4.x on Bedrock requires a cross-region inference profile; "
        "pass the explicit form to silence this warning.",
        model,
        rewritten,
        region or "<unset>",
    )
    return rewritten


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
    model = _normalize_bedrock_model_id(model)
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
