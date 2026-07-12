"""Shared helpers for resolving and inspecting chat models."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


_PROVIDER_ALIASES: dict[str, str] = {
    "azure_openai": "azure",
    "mistralai": "mistral",
}
"""Known provider aliases between LangChain specs and LangSmith params.

LangChain `provider:model` specs and the `ls_provider` reported by
`_get_ls_params` use different provider names for some integrations.
Canonicalize only known aliases before comparing providers.
"""

_BEDROCK_PROVIDERS: frozenset[str] = frozenset({"amazon_bedrock", "anthropic_bedrock", "aws", "bedrock", "bedrock_converse"})
"""Normalized provider names that identify AWS Bedrock chat models."""

_BEDROCK_MODEL_CLASSES: frozenset[str] = frozenset({"ChatAnthropicBedrock", "ChatBedrock", "ChatBedrockConverse", "ChatBedrockNovaSonic"})
"""`langchain-aws` chat model class names that identify AWS Bedrock models."""

_BEDROCK_REGIONAL_PREFIXES: tuple[str, ...] = ("apac.", "amer.", "au.", "eu.", "global.", "jp.", "sa.", "us.", "us-gov.")
"""Regional inference profile prefixes stripped from Bedrock model identifiers.

Distinct from `_BEDROCK_PROFILE_PREFIXES` (used by the bare-id auto-resolver):
this wider set is used only to peel a regional prefix off a Bedrock model id
before checking for a cache-capable Nova identifier.
"""


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
    if timeout_secs is None:
        return kwargs
    if model.startswith(("bedrock:", "bedrock_converse:")):
        kwargs.update(_bedrock_config_kwarg(timeout_secs))
    else:
        # Anthropic, OpenAI, Gemini, Mistral, Cohere, Groq, DeepSeek, etc. all
        # accept a flat `timeout=` kwarg that flows down to their HTTP client.
        kwargs["timeout"] = timeout_secs
    return kwargs


def _apply_openai_responses_default(model: str, kwargs: dict[str, Any]) -> None:
    """Default OpenAI specs to the Responses API unless a profile decided.

    Historically `_model_init_kwargs` hardcoded `use_responses_api=True` for
    every `openai:` spec, which landed in the caller-kwargs layer that wins
    over `apply_provider_profile` — making a `ProviderProfile` override (e.g.
    `ProviderProfile(init_kwargs={"use_responses_api": False})`) permanently
    dead. The default now sits **beneath** the profile: it is only applied when
    neither a registered profile nor the caller has already set the key, so a
    profile can turn the Responses API off (or on). Behavior for OpenAI users
    is unchanged when no profile is registered — the SDK still defaults them to
    the Responses API.

    Mutates `kwargs` in place.

    Args:
        model: Model spec like `provider:identifier`.
        kwargs: Merged `init_chat_model` kwargs (post profile application).
    """
    if model.startswith("openai:") and "use_responses_api" not in kwargs:
        kwargs["use_responses_api"] = True


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
    # Layer any registered `ProviderProfile` for this spec underneath the
    # computed kwargs (caller/SDK kwargs win on conflicts). This runs the
    # profile's `pre_init` hook and merges its `init_kwargs` /
    # `init_kwargs_factory` output. Imported locally so `import bog_agents`
    # stays cheap and to avoid any import-cycle through the profiles package.
    from bog_agents.profiles.provider.provider_profiles import apply_provider_profile

    kwargs = apply_provider_profile(model, kwargs)
    _apply_openai_responses_default(model, kwargs)
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


def get_model_provider(model: BaseChatModel) -> str | None:
    """Extract the LangChain provider identifier from a chat model.

    Reads the model's LangSmith params (`ls_provider`), which providers
    populate with their canonical short name (e.g. `anthropic`, `openai`,
    `bedrock_converse`). Used by the harness-profile lookup to resolve a
    `provider:model` key when the caller passes a pre-built `BaseChatModel`
    instead of a `provider:model` string.

    Args:
        model: Chat model instance to inspect.

    Returns:
        The provider short name, or `None` when it cannot be determined.
    """
    try:
        params = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError) as exc:
        # A missing or raising `_get_ls_params` causes profile resolution to
        # silently miss for this model. Log at INFO (not DEBUG) so custom
        # integrations can debug "my profile isn't applying" without turning on
        # DEBUG. Narrowed from a bare `except`: only the shapes a partial or
        # unimplemented `_get_ls_params` actually raises are swallowed; any
        # other error surfaces instead of being silently mapped to `None`.
        logger.info(
            "Could not extract provider from %s.%s via _get_ls_params: %s",
            type(model).__module__,
            type(model).__name__,
            exc,
        )
        return None
    if not isinstance(params, Mapping):
        # A custom integration may return `None` (or another non-mapping)
        # instead of raising. Treat that as "provider unavailable" rather than
        # letting the subsequent `.get` raise `AttributeError`.
        logger.info(
            "Could not extract provider from %s.%s: _get_ls_params returned %s, not a mapping",
            type(model).__module__,
            type(model).__name__,
            type(params).__name__,
        )
        return None
    provider = params.get("ls_provider")
    if isinstance(provider, str) and provider:
        return provider
    return None


def is_bedrock_model(model: str | BaseChatModel) -> bool:
    """Check whether a model targets AWS Bedrock.

    For string specs, the provider half (before the first colon) is normalized
    and checked against the known Bedrock provider names, and bare Nova
    identifiers (which may omit a provider prefix) are recognised directly. For
    instances, the provider reported by `get_model_provider` is checked first,
    then the concrete `langchain-aws` chat model class name as a fallback.

    Args:
        model: Model spec in `provider:model` format, or a chat model instance.

    Returns:
        `True` if the model targets AWS Bedrock, otherwise `False`.
    """
    if isinstance(model, str):
        if _is_bedrock_nova_model_id(model):
            return True
        provider, separator, _ = model.partition(":")
        return bool(separator) and _normalize_provider(provider) in _BEDROCK_PROVIDERS

    provider = get_model_provider(model)
    if provider is not None and _normalize_provider(provider) in _BEDROCK_PROVIDERS:
        return True
    return type(model).__name__ in _BEDROCK_MODEL_CLASSES


def _is_bedrock_nova_model_id(model: str) -> bool:
    """Check for a cache-capable Bedrock Nova model identifier.

    Peels a single regional inference-profile prefix (e.g. `us.`) off the id,
    then tests for the `amazon.nova-` family.

    Args:
        model: Model spec or bare model identifier.

    Returns:
        `True` when the identifier names a Bedrock Nova model.
    """
    identifier = model
    for prefix in _BEDROCK_REGIONAL_PREFIXES:
        if identifier.startswith(prefix):
            identifier = identifier.removeprefix(prefix)
            break
    return identifier.startswith("amazon.nova-")


def _normalize_provider(provider: str) -> str:
    """Canonicalize a provider name so equal providers compare equal.

    Specs use the `provider:model` spelling (lowercase, underscore-separated,
    e.g. `azure_openai`), while the `ls_provider` reported by `_get_ls_params`
    may differ in case, use hyphens (`openai-codex`), or use an entirely
    different name (`mistralai` vs `mistral`). Folding both sides through this
    function before comparison keeps those spellings from reading as a mismatch.

    Args:
        provider: Raw provider name from either a spec or `ls_provider`.

    Returns:
        The canonical provider name.
    """
    normalized = provider.lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(normalized, normalized)


def model_matches_spec(model: BaseChatModel, spec: str) -> bool:
    """Check whether a model instance already matches a string model spec.

    Bare specs (no colon) match by model identifier alone. Provider-prefixed
    specs must match on **both** the model identifier and the provider: a spec
    like `"openai:gpt-5"` no longer matches an Anthropic model that merely
    happens to expose a `"gpt-5"` identifier. When the current model's provider
    cannot be inspected (`_get_ls_params` unavailable), the check falls back to
    identifier-only matching for backwards compatibility with custom models.
    Provider comparison is normalized (see `_normalize_provider`), so case,
    hyphen/underscore spelling, and known aliases do not read as a mismatch.

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

    provider, separator, model_name = spec.partition(":")
    if not separator or model_name != current:
        return False

    current_provider = get_model_provider(model)
    if current_provider is None:
        # Provider could not be inspected, so the spec's provider cannot be
        # confirmed. Fall back to the identifier-only match. Logged at DEBUG so
        # a consumer skipping a model swap on the strength of this match (e.g.
        # the runtime model override) is traceable when it surprises.
        logger.debug(
            "Matched spec %r on identifier alone; provider for %s.%s is uninspectable, so the spec's %r provider was not verified",
            spec,
            type(model).__module__,
            type(model).__name__,
            provider,
        )
        return True
    return _normalize_provider(provider) == _normalize_provider(current_provider)


def _string_value(config: dict[str, Any], key: str) -> str | None:
    """Return a non-empty string value from a serialized model config."""
    value = config.get(key)
    if isinstance(value, str) and value:
        return value
    return None
