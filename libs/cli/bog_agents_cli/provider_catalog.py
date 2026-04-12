"""Shared provider metadata for model detection, defaults, and supplements."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

OPENAI_DETECTION_EXACT: frozenset[str] = frozenset(
    {
        "codex-mini-latest",
    }
)
"""Bare model names that should route to the OpenAI provider."""

OPENAI_DETECTION_PREFIXES: tuple[str, ...] = (
    "chatgpt",
    "codex-",
    "gpt-",
    "gpt-oss-",
    "o1",
    "o3",
    "o4",
)
"""Prefixes that indicate an OpenAI-hosted model."""

BEDROCK_VENDOR_PREFIXES: tuple[str, ...] = (
    "ai21.",
    "amazon.",
    "anthropic.",
    "cohere.",
    "deepseek.",
    "meta.",
    "mistral.",
    "writer.",
)
"""Vendor prefixes used by direct Amazon Bedrock model IDs."""

BEDROCK_REGION_PREFIXES: tuple[str, ...] = (
    "apac.",
    "eu.",
    "global.",
    "jp.",
    "sa.",
    "us.",
)
"""Cross-region prefixes used by Bedrock inference profile IDs."""

DEFAULT_MODEL_CANDIDATES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "anthropic": (
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-sonnet-4-20250514",
        ),
        "azure_openai": (
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5",
        ),
        "bedrock_converse": (
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "anthropic.claude-opus-4-1-20250805-v1:0",
            "anthropic.claude-3-7-sonnet-20250219-v1:0",
        ),
        "google_genai": (
            "gemini-2.5-pro",
            "gemini-3-pro-preview",
            "gemini-2.5-flash",
        ),
        "google_vertexai": (
            "gemini-2.5-pro",
            "gemini-3-pro-preview",
            "gemini-2.5-flash",
        ),
        "nvidia": (
            "nvidia/nemotron-3-super-120b-a12b",
            "nemotron-3-nano-30b-a3b",
        ),
        "ollama": (
            "qwen3:4b",
            "llama3",
        ),
        "openai": (
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5",
        ),
    }
)
"""Recommended model IDs, ordered from most to least preferred."""

_OPENAI_GPT_5_4_FAMILY: dict[str, dict[str, Any]] = {
    "gpt-5.4": {
        "max_input_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "text_outputs": True,
        "image_inputs": True,
        "audio_inputs": False,
        "video_inputs": False,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
    "gpt-5.4-mini": {
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "text_outputs": True,
        "image_inputs": True,
        "audio_inputs": False,
        "video_inputs": False,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
    "gpt-5.4-nano": {
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "text_outputs": True,
        "image_inputs": True,
        "audio_inputs": False,
        "video_inputs": False,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
}

SUPPLEMENTAL_MODEL_PROFILES: Mapping[str, Mapping[str, dict[str, Any]]] = (
    MappingProxyType(
        {
            "azure_openai": MappingProxyType(_OPENAI_GPT_5_4_FAMILY),
            "openai": MappingProxyType(_OPENAI_GPT_5_4_FAMILY),
        }
    )
)
"""Curated profile entries for provider/model IDs not in installed profiles yet."""

PROFILE_OVERRIDES: Mapping[str, Mapping[str, dict[str, Any]]] = MappingProxyType(
    {
        "openai": MappingProxyType(
            {
                "codex-mini-latest": {
                    "status": "deprecated",
                }
            }
        )
    }
)
"""Curated overrides for installed profile entries that need fresher metadata."""


def detects_openai_model(model_name: str) -> bool:
    """Return whether a bare model name should route to OpenAI."""
    model_lower = model_name.lower()
    return model_lower in OPENAI_DETECTION_EXACT or model_lower.startswith(
        OPENAI_DETECTION_PREFIXES
    )


def is_bedrock_model_id(model_name: str) -> bool:
    """Return whether a bare model name looks like an Amazon Bedrock ID."""
    model_lower = model_name.lower()
    if model_lower.startswith(BEDROCK_VENDOR_PREFIXES):
        return True
    return any(
        model_lower.startswith(f"{region}{vendor}")
        for region in BEDROCK_REGION_PREFIXES
        for vendor in BEDROCK_VENDOR_PREFIXES
    )


def get_default_model_candidates(provider: str) -> Sequence[str]:
    """Return the preferred default-model order for a provider."""
    return DEFAULT_MODEL_CANDIDATES.get(provider, ())


def choose_preferred_model(
    provider: str, available_models: Sequence[str] | None = None
) -> str | None:
    """Choose the best model for a provider from available and fallback options."""
    candidates = get_default_model_candidates(provider)
    if not candidates:
        return None

    if available_models:
        available = set(available_models)
        for candidate in candidates:
            if candidate in available:
                return candidate

    return candidates[0]


def get_supplemental_model_profiles(provider: str) -> Mapping[str, dict[str, Any]]:
    """Return curated model profiles for a provider."""
    return SUPPLEMENTAL_MODEL_PROFILES.get(provider, MappingProxyType({}))


def get_profile_overrides(provider: str) -> Mapping[str, dict[str, Any]]:
    """Return curated profile overrides for an installed provider profile set."""
    return PROFILE_OVERRIDES.get(provider, MappingProxyType({}))
