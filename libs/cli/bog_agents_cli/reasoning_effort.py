"""Provider-specific reasoning effort support for `/effort`.

The legacy `/effort` implementation mapped `low/medium/high/max` onto
`{max_tokens, temperature}` — a hack that actively truncates a modern
reasoning model (capping output at 1024 tokens on `low`) and perturbs
sampling temperature. This module replaces that with per-provider,
per-model translation onto each vendor's real reasoning knob:

- Anthropic — `output_config.effort` (plus an adaptive `thinking` block)
- OpenAI — `reasoning.effort`
- Gemini — `thinking_level`
- Fireworks — `reasoning_effort` (via `model_kwargs`)
- xAI — `reasoning_effort` (via `extra_body`)

Capability detection is per model/version: only the effort levels a given
model actually accepts are offered (e.g. `xhigh` exists on Opus 4.7+ but not
Opus 4.6, and Sonnet 4.5 rejects `effort` entirely).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeAlias, get_args

from bog_agents_cli.model_config import ModelSpec

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

EffortLabel: TypeAlias = Literal["none", "low", "medium", "high", "xhigh", "max"]
"""Closed vocabulary of effort labels across all supported providers.

Typing the per-provider tuples with this alias catches typos in the vocabulary
at check time. It does not express the deeper invariant that a label must be
supported by a *specific* model — that is enforced at runtime by
`supported_efforts_for_model`.
"""

ReasoningProvider: TypeAlias = Literal[
    "anthropic", "bedrock", "fireworks", "google_genai", "openai", "xai"
]
"""Provider identifiers that support model-specific reasoning effort controls.

Values must stay byte-identical to the provider strings from `ModelSpec.parse`
used throughout `model_config.py`. `bedrock` covers both the `bedrock` and
`bedrock_converse` provider strings for Anthropic-on-Bedrock models.
"""

LEGACY_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")
"""Fallback `/effort` vocabulary for models with no native reasoning knob.

These map to `{temperature}` presets in `configurable_model.py`. They never cap
`max_tokens`: capping output was a truncation hack (RD-1 / v4) that cut off a
model mid-response — including operator-routed `easy`-tier turns at 1024 tokens.
A non-reasoning model is left at its natural output length; only its sampling
temperature is nudged by effort.
"""

EFFORT_DESCRIPTIONS: dict[str, str] = {
    "none": "Reasoning off — fastest, no deliberate thinking.",
    "low": "Quick responses with minimal reasoning overhead.",
    "medium": "Balanced reasoning and speed.",
    "high": "Thorough analysis for most coding tasks.",
    "xhigh": "Extended reasoning for hard, multi-step problems.",
    "max": "Maximum reasoning depth for the most complex work.",
}
"""Human-readable blurb per effort label, shown by `/effort` with no argument."""


class ReasoningProviderConfig(NamedTuple):
    """Provider-specific reasoning effort behavior."""

    supported_efforts: Callable[[str], tuple[EffortLabel, ...]]
    """Return supported effort labels for a lowercased model name."""

    default_effort: Callable[[str], EffortLabel | None]
    """Return the provider default effort for a lowercased model name, if known."""

    model_params: Callable[[str], dict[str, Any]]
    """Translate an effort label into provider-specific model params."""


OPENAI_EFFORTS: tuple[EffortLabel, ...] = ("none", "low", "medium", "high", "xhigh")
"""OpenAI GPT-5 effort labels before GPT-5.6 for `reasoning.effort`.

See https://platform.openai.com/docs/guides/reasoning.
"""

OPENAI_56_EFFORTS: tuple[EffortLabel, ...] = (*OPENAI_EFFORTS, "max")
"""OpenAI GPT-5.6 effort labels for `reasoning.effort`."""

ANTHROPIC_EFFORTS: tuple[EffortLabel, ...] = ("low", "medium", "high", "xhigh", "max")
"""Anthropic `output_config.effort` labels for Opus 4.7+ and Sonnet 5.

See https://platform.claude.com/docs/en/build-with-claude/effort.
"""

ANTHROPIC_EFFORTS_NO_XHIGH: tuple[EffortLabel, ...] = ("low", "medium", "high", "max")
"""Anthropic effort labels for Opus 4.6 and Sonnet 4.6.

These models predate `xhigh`; Sonnet 4.5 rejects `effort` entirely.
"""

ANTHROPIC_EFFORTS_NO_MAX: tuple[EffortLabel, ...] = ("low", "medium", "high")
"""Anthropic effort labels for Opus 4.5 (predates both `max` and `xhigh`)."""

GOOGLE_EFFORTS: tuple[EffortLabel, ...] = ("low", "medium", "high")
"""Gemini `thinking_level` labels for `gemini-3*` models.

See https://ai.google.dev/gemini-api/docs/thinking.
"""

FIREWORKS_REASONING_EFFORTS: tuple[EffortLabel, ...] = (
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
"""Fireworks `reasoning_effort` labels for DeepSeek V4 Pro.

See https://docs.fireworks.ai/guides/reasoning.
"""

FIREWORKS_KIMI_EFFORTS: tuple[EffortLabel, ...] = ("low", "medium", "high")
"""Fireworks `reasoning_effort` labels for Kimi K2 models."""

FIREWORKS_GLM_EFFORTS: tuple[EffortLabel, ...] = ("none", "high", "max")
"""Fireworks `reasoning_effort` labels for GLM 5 models."""

XAI_EFFORTS: tuple[EffortLabel, ...] = ("low", "medium", "high")
"""xAI `reasoning_effort` labels for Grok 4.5.

See https://docs.x.ai/developers/model-capabilities/text/reasoning.
"""


def _has_version(model: str, token: str) -> bool:
    """Return whether `model` carries version `token` not followed by a digit.

    A plain substring test would match a longer version by accident — e.g.
    `"opus-4-1" in "claude-opus-4-16"` is true. Anchoring on a non-digit
    boundary keeps `opus-4-1` from matching a future `opus-4-16` while still
    matching a dated suffix like `opus-4-1-20250805`. `token` is always a
    hardcoded constant, but `re.escape` keeps the match literal regardless.
    """
    return re.search(rf"{re.escape(token)}(?!\d)", model) is not None


def _openai_supported_efforts(model: str) -> tuple[EffortLabel, ...]:
    """Return OpenAI reasoning effort levels."""
    return OPENAI_56_EFFORTS if _has_version(model, "gpt-5.6") else OPENAI_EFFORTS


def _openai_default_effort(model: str) -> EffortLabel | None:
    """Return the OpenAI default reasoning effort when known."""
    if _has_version(model, "gpt-5.5") or _has_version(model, "gpt-5.6"):
        return "medium"
    return None


def _openai_model_params(effort: str) -> dict[str, Any]:
    """Return OpenAI reasoning params for an effort label."""
    if effort == "none":
        return {"reasoning": {"effort": "none"}}
    return {"reasoning": {"effort": effort, "summary": "auto"}}


def _anthropic_supported_efforts(model: str) -> tuple[EffortLabel, ...]:
    """Return the effort levels an Anthropic model accepts.

    Args:
        model: Lowercased Anthropic model name (e.g. `claude-opus-4-8`).

    Returns:
        Supported effort labels, or an empty tuple when the model does not
        accept `effort` (e.g. Sonnet 4.5).
    """
    if model.startswith("claude-opus-"):
        if _has_version(model, "opus-4-0") or _has_version(model, "opus-4-1"):
            # Opus 4.0/4.1 predate reasoning effort entirely.
            return ()
        if _has_version(model, "opus-4-5"):
            # Opus 4.5 predates both `max` (4.6+) and `xhigh` (4.7+).
            return ANTHROPIC_EFFORTS_NO_MAX
        # Opus 4.6 predates `xhigh`; 4.7+ (and newer, unrecognized versions)
        # get the full range.
        return (
            ANTHROPIC_EFFORTS_NO_XHIGH
            if _has_version(model, "opus-4-6")
            else ANTHROPIC_EFFORTS
        )
    if model.startswith("claude-sonnet-"):
        if (
            _has_version(model, "sonnet-4-0")
            or _has_version(model, "sonnet-4-1")
            or _has_version(model, "sonnet-4-5")
        ):
            # Sonnet 4.0/4.1 predate effort; Sonnet 4.5 rejects it.
            return ()
        # Sonnet 4.6 predates `xhigh`; Sonnet 5 (and newer) get the full range.
        return (
            ANTHROPIC_EFFORTS_NO_XHIGH
            if _has_version(model, "sonnet-4-6")
            else ANTHROPIC_EFFORTS
        )
    return ()


def _anthropic_default_effort(model: str) -> EffortLabel | None:
    """Return the Anthropic default reasoning effort when known."""
    return "high" if _anthropic_supported_efforts(model) else None


def _anthropic_model_params(effort: str) -> dict[str, Any]:
    """Return Anthropic reasoning params for an effort label."""
    return {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": effort},
    }


def _normalize_bedrock_anthropic_model(model: str) -> str | None:
    """Return the underlying `claude-...` name for a Bedrock Anthropic id.

    Bedrock ids embed the Anthropic model behind a vendor (and optional region)
    prefix and a version/date suffix — e.g. `us.anthropic.claude-opus-4-6-20250101-v1:0`
    or the bare `anthropic.claude-opus-4-6` used by the operator preset. Slicing
    from `claude-` lets the Anthropic effort tables (which match on
    `claude-opus-`/`claude-sonnet-` plus a version token) apply unchanged.

    Args:
        model: Lowercased Bedrock model id.

    Returns:
        The `claude-...` slice, or `None` when the id is not an Anthropic model.
    """
    idx = model.find("claude-")
    if idx == -1:
        return None
    return model[idx:]


def _bedrock_supported_efforts(model: str) -> tuple[EffortLabel, ...]:
    """Return effort levels for a Bedrock Anthropic model (Opus/Sonnet).

    Delegates to the Anthropic version-gated tables so a Bedrock Claude id gets
    exactly the levels its underlying model accepts. Non-Anthropic and
    non-reasoning Bedrock ids (e.g. Haiku) yield an empty tuple.
    """
    normalized = _normalize_bedrock_anthropic_model(model)
    if normalized is None:
        return ()
    return _anthropic_supported_efforts(normalized)


def _bedrock_default_effort(model: str) -> EffortLabel | None:
    """Return the Bedrock Anthropic default effort when known."""
    normalized = _normalize_bedrock_anthropic_model(model)
    if normalized is None:
        return None
    return _anthropic_default_effort(normalized)


def _bedrock_model_params(effort: str) -> dict[str, Any]:
    """Return Bedrock reasoning params for an effort label.

    `ChatBedrockConverse` forwards provider-specific fields to the underlying
    Anthropic model via `additional_model_request_fields`, so the same
    `thinking` + `output_config.effort` payload the direct Anthropic path uses
    is nested there rather than passed as top-level invocation kwargs.
    """
    return {"additional_model_request_fields": _anthropic_model_params(effort)}


def _google_supported_efforts(_model: str) -> tuple[EffortLabel, ...]:
    """Return Gemini thinking levels."""
    return GOOGLE_EFFORTS


def _google_default_effort(model: str) -> EffortLabel | None:
    """Return the Gemini default thinking level when known."""
    if model.startswith("gemini-3.5-flash"):
        return "medium"
    if model.startswith(("gemini-3.1-pro", "gemini-3-flash", "gemini-3-pro")):
        return "high"
    return None


def _google_model_params(effort: str) -> dict[str, Any]:
    """Return Gemini thinking params for an effort label."""
    return {"thinking_level": effort}


def _fireworks_supported_efforts(model: str) -> tuple[EffortLabel, ...]:
    """Return Fireworks reasoning effort levels for a model."""
    if "kimi-k2" in model:
        return FIREWORKS_KIMI_EFFORTS
    if "glm-5" in model:
        return FIREWORKS_GLM_EFFORTS
    if "deepseek-v4-pro" in model:
        return FIREWORKS_REASONING_EFFORTS
    return ()


def _fireworks_default_effort(model: str) -> EffortLabel | None:
    """Return the Fireworks default reasoning effort when known."""
    if "deepseek-v4-pro" in model:
        return "high"
    if "glm-5p2" in model:
        return "max"
    return None


def _fireworks_model_params(effort: str) -> dict[str, Any]:
    """Return Fireworks reasoning params for an effort label."""
    return {"model_kwargs": {"reasoning_effort": effort}}


def _is_xai_grok_45(model: str) -> bool:
    """Return whether `model` is Grok 4.5 or a documented alias."""
    return model in {"grok-4.5", "grok-4.5-latest", "grok-build-latest"}


def _xai_supported_efforts(model: str) -> tuple[EffortLabel, ...]:
    """Return xAI reasoning effort levels for a model."""
    return XAI_EFFORTS if _is_xai_grok_45(model) else ()


def _xai_default_effort(model: str) -> EffortLabel | None:
    """Return the xAI default reasoning effort when known."""
    return "high" if _is_xai_grok_45(model) else None


def _xai_model_params(effort: str) -> dict[str, Any]:
    """Return xAI reasoning params for an effort label."""
    return {"extra_body": {"reasoning_effort": effort}}


_PROVIDER_CONFIGS: dict[ReasoningProvider, ReasoningProviderConfig] = {
    "openai": ReasoningProviderConfig(
        supported_efforts=_openai_supported_efforts,
        default_effort=_openai_default_effort,
        model_params=_openai_model_params,
    ),
    "anthropic": ReasoningProviderConfig(
        supported_efforts=_anthropic_supported_efforts,
        default_effort=_anthropic_default_effort,
        model_params=_anthropic_model_params,
    ),
    "bedrock": ReasoningProviderConfig(
        supported_efforts=_bedrock_supported_efforts,
        default_effort=_bedrock_default_effort,
        model_params=_bedrock_model_params,
    ),
    "google_genai": ReasoningProviderConfig(
        supported_efforts=_google_supported_efforts,
        default_effort=_google_default_effort,
        model_params=_google_model_params,
    ),
    "fireworks": ReasoningProviderConfig(
        supported_efforts=_fireworks_supported_efforts,
        default_effort=_fireworks_default_effort,
        model_params=_fireworks_model_params,
    ),
    "xai": ReasoningProviderConfig(
        supported_efforts=_xai_supported_efforts,
        default_effort=_xai_default_effort,
        model_params=_xai_model_params,
    ),
}
"""Provider-specific reasoning effort behavior keyed by `ModelSpec` provider."""

if set(_PROVIDER_CONFIGS) != set(get_args(ReasoningProvider)):  # pragma: no cover
    # `_classify_reasoning_provider` only ever returns members of the
    # `ReasoningProvider` vocabulary, and `_reasoning_config` indexes
    # `_PROVIDER_CONFIGS` with the result — so the two must stay in lockstep or
    # that lookup raises `KeyError` at runtime. Fail loudly at import instead.
    msg = "_PROVIDER_CONFIGS keys must match the ReasoningProvider vocabulary"
    raise RuntimeError(msg)


def _classify_reasoning_provider(provider: str, model: str) -> ReasoningProvider | None:
    """Classify provider/model parts into a reasoning-capable provider.

    Returns:
        The registry key for supported reasoning models, or `None` otherwise.
    """
    model_lower = model.lower()
    if provider == "openai" and model_lower.startswith("gpt-5"):
        return "openai"
    if provider == "anthropic" and model_lower.startswith(
        ("claude-opus-", "claude-sonnet-")
    ):
        return "anthropic"
    if provider in ("bedrock", "bedrock_converse") and _bedrock_supported_efforts(
        model_lower
    ):
        # Only Anthropic Opus/Sonnet on Bedrock have a native effort knob; Haiku
        # and non-Anthropic ids fall through to None (non-reasoning), which no
        # longer caps output (see configurable_model._EFFORT_LEVEL_SETTINGS).
        return "bedrock"
    if provider == "google_genai" and model_lower.startswith("gemini-3"):
        return "google_genai"
    if provider == "fireworks" and model_lower.startswith("accounts/fireworks/models/"):
        return "fireworks"
    if provider == "xai" and _is_xai_grok_45(model_lower):
        return "xai"
    return None


def _reasoning_config(model_spec: str) -> tuple[ReasoningProviderConfig, str] | None:
    """Return provider config and lowercased model when reasoning is supported."""
    parsed = ModelSpec.try_parse(model_spec)
    if parsed is None:
        return None
    provider = _classify_reasoning_provider(parsed.provider, parsed.model)
    if provider is None:
        return None
    return _PROVIDER_CONFIGS[provider], parsed.model.lower()


def supported_efforts_for_model(model_spec: str | None) -> tuple[str, ...]:
    """Return native reasoning efforts supported by `model_spec`.

    Returns plain `str` labels rather than `EffortLabel`: this is the public
    boundary where the label vocabulary is intentionally dropped, since the
    values flow straight to the UI. An empty tuple means the model has no
    native reasoning knob (a *non-reasoning* model) — callers must never cap
    its output; see `configurable_model._apply_overrides`.

    Args:
        model_spec: `provider:model` spec for the active model.

    Returns:
        Supported effort labels, or an empty tuple when the model is unsupported.
    """
    if not model_spec:
        return ()
    context = _reasoning_config(model_spec)
    if context is None:
        return ()
    config, model = context
    efforts = config.supported_efforts(model)
    if not efforts:
        # A recognized reasoning provider that yields no configurable efforts
        # usually means the model-version heuristics need updating for a newer
        # release. Log at info so the maintenance gap is visible at default
        # verbosity rather than silently reporting "not configurable".
        logger.info("No configurable reasoning efforts for %s", model_spec)
    return efforts


def default_effort_for_model(model_spec: str | None) -> str | None:
    """Return the documented default reasoning effort when known.

    Args:
        model_spec: `provider:model` spec for the active model.

    Returns:
        The provider default effort label, or `None` when the default is unknown.
    """
    if not model_spec:
        return None
    context = _reasoning_config(model_spec)
    if context is None:
        return None
    config, model = context
    return config.default_effort(model)


def model_params_for_effort(
    model_spec: str | None, effort: str
) -> dict[str, Any] | None:
    """Translate an effort label into provider-specific model params.

    Args:
        model_spec: `provider:model` spec for the active model.
        effort: Effort label to translate.

    Returns:
        Native model params to merge into `model_settings`, or `None` when the
        model has no native reasoning knob or does not accept `effort`. A
        `None` result must never be treated as "cap the output" for a model
        whose `supported_efforts_for_model` is non-empty.
    """
    if not model_spec:
        return None
    context = _reasoning_config(model_spec)
    if context is None:
        return None
    config, model = context
    if effort not in config.supported_efforts(model):
        return None
    return config.model_params(effort)


def effort_levels_for_model(model_spec: str | None) -> tuple[str, ...]:
    """Return the `/effort` vocabulary valid for `model_spec`.

    For a reasoning model this is its native supported set; for any other
    model it is the legacy `low/medium/high/max` preset vocabulary (which
    `configurable_model` translates to a `{temperature}` preset). This is
    the set the `/effort` command validates a requested level against.

    Args:
        model_spec: `provider:model` spec for the active model.

    Returns:
        The ordered tuple of valid effort labels (never empty).
    """
    native = supported_efforts_for_model(model_spec)
    return native or LEGACY_EFFORT_LEVELS


def render_effort_status(model_spec: str | None, current: str) -> str:
    """Render the `/effort` status block shown when called with no argument.

    Args:
        model_spec: `provider:model` spec for the active model.
        current: The currently selected effort level.

    Returns:
        A multi-line string listing the current level, each valid level with
        its description, and usage — tailored to whether the active model has
        a native reasoning knob.
    """
    levels = effort_levels_for_model(model_spec)
    native = bool(supported_efforts_for_model(model_spec))
    lines = [f"Current effort: {current}", ""]
    if native:
        default = default_effort_for_model(model_spec)
        suffix = f" (model default: {default})" if default else ""
        lines.append(f"Native reasoning levels for this model{suffix}:")
    else:
        lines.append(
            "This model has no native reasoning control; levels map to "
            "token/temperature presets:"
        )
    lines.extend(
        f"  {level} - {EFFORT_DESCRIPTIONS.get(level, '')}".rstrip() for level in levels
    )
    lines.append("")
    lines.append(f"Usage: /effort {'|'.join(levels)}")
    return "\n".join(lines)
