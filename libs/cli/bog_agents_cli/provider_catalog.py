"""Shared provider metadata for model detection, defaults, and supplements."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

logger = logging.getLogger(__name__)

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
            # Live API IDs from platform.claude.com/docs/en/docs/about-claude/models
            # (fetched 2026-04-30). Latest first.
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-opus-4-6",
            "claude-sonnet-4-5",
            "claude-opus-4-5",
            "claude-opus-4-1",
        ),
        "azure_openai": (
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5",
        ),
        "bedrock": (
            # AWS Bedrock model IDs, refreshed 2026-06-17.
            #
            # Anthropic Claude 4.x on Bedrock REQUIRES a cross-region
            # inference profile prefix (us./eu./apac./...); the bare
            # `anthropic.claude-opus-4-8` IDs return AccessDenied even
            # when the account has model access granted. The auto-
            # resolver in `bog_agents._models` rewrites bare→regional
            # based on AWS_REGION as a safety net, but the catalog lists
            # the regional profile IDs directly so the picker surfaces
            # them as first-class entries. See docs/providers/bedrock.md.
            #
            # ─── ANTHROPIC CLAUDE (inference profiles required) ─────
            "us.anthropic.claude-opus-4-8",
            "eu.anthropic.claude-opus-4-8",
            "apac.anthropic.claude-opus-4-8",
            "us.anthropic.claude-opus-4-7",
            "eu.anthropic.claude-opus-4-7",
            "apac.anthropic.claude-opus-4-7",
            "us.anthropic.claude-sonnet-4-6",
            "eu.anthropic.claude-sonnet-4-6",
            "apac.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "apac.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.anthropic.claude-opus-4-1-20250805-v1:0",
            # ─── AMAZON NOVA ─────────────────────────────────────────
            "us.amazon.nova-premier-v1:0",
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-micro-v1:0",
            # ─── META LLAMA ──────────────────────────────────────────
            "us.meta.llama4-maverick-17b-instruct-v1:0",
            "us.meta.llama4-scout-17b-instruct-v1:0",
            "us.meta.llama3-3-70b-instruct-v1:0",
            # ─── MISTRAL ─────────────────────────────────────────────
            "us.mistral.mistral-large-3-2411-v1:0",
            "us.mistral.pixtral-large-2502-v1:0",
        ),
        # bedrock_converse is the modern Converse API wrapper. Same model
        # IDs as bedrock (above) plus DeepSeek which is converse-only.
        "bedrock_converse": (
            # ─── ANTHROPIC CLAUDE (inference profiles required) ─────
            "us.anthropic.claude-opus-4-8",
            "eu.anthropic.claude-opus-4-8",
            "apac.anthropic.claude-opus-4-8",
            "us.anthropic.claude-opus-4-7",
            "eu.anthropic.claude-opus-4-7",
            "apac.anthropic.claude-opus-4-7",
            "us.anthropic.claude-sonnet-4-6",
            "eu.anthropic.claude-sonnet-4-6",
            "apac.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "apac.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.anthropic.claude-opus-4-1-20250805-v1:0",
            # ─── AMAZON NOVA ─────────────────────────────────────────
            "us.amazon.nova-premier-v1:0",
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-micro-v1:0",
            # ─── META LLAMA ──────────────────────────────────────────
            "us.meta.llama4-maverick-17b-instruct-v1:0",
            "us.meta.llama4-scout-17b-instruct-v1:0",
            "us.meta.llama3-3-70b-instruct-v1:0",
            # ─── MISTRAL ─────────────────────────────────────────────
            "us.mistral.mistral-large-3-2411-v1:0",
            "us.mistral.pixtral-large-2502-v1:0",
            # ─── DEEPSEEK (converse-only) ────────────────────────────
            "us.deepseek.deepseek-r1-v1:0",
        ),
        "google_genai": (
            # Live IDs from ai.google.dev/gemini-api/docs/models
            # (fetched 2026-04-30). Preview models clearly marked.
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
        ),
        "google_vertexai": (
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
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
"""Recommended model IDs, ordered from most to least preferred.

Bedrock entries include both base model IDs (``anthropic.claude-...``) and
their cross-region inference profile counterparts (``us.anthropic.claude-...``).
Use the regional profile when calling from a US-based region for higher
quotas; the base IDs work in single-region calls.
"""

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


def clear_provider_catalog_caches() -> None:
    """Clear module-level caches used for provider discovery."""
    get_local_ollama_models.cache_clear()


def _normalize_ollama_host(raw_host: str | None) -> str:
    """Normalize the configured Ollama host into an HTTP base URL."""
    host = (raw_host or "").strip() or "http://127.0.0.1:11434"
    if "://" not in host:
        host = f"http://{host}"
    parsed = urlparse(host)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    return f"{scheme}://{netloc}{path}".rstrip("/")


def _dedupe_preserving_order(items: Sequence[str]) -> tuple[str, ...]:
    """Return unique strings in the order they first appeared."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(cleaned)
    return tuple(result)


def _fetch_ollama_models_via_http() -> tuple[str, ...]:
    """Read the local Ollama model list from the daemon HTTP API."""
    base_url = _normalize_ollama_host(os.environ.get("OLLAMA_HOST"))
    try:
        with urlopen(f"{base_url}/api/tags", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return ()

    models = payload.get("models")
    if not isinstance(models, list):
        return ()

    names = [
        str(model.get("name", "")).strip()
        for model in models
        if isinstance(model, dict)
    ]
    return _dedupe_preserving_order(names)


def _fetch_ollama_models_via_cli() -> tuple[str, ...]:
    """Read the local Ollama model list via `ollama list`."""
    if shutil.which("ollama") is None:
        return ()

    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ()

    if result.returncode != 0:
        return ()

    lines = result.stdout.splitlines()
    names = [parts[0] for line in lines[1:] if (parts := line.split())]
    return _dedupe_preserving_order(names)


@lru_cache(maxsize=1)
def get_local_ollama_models() -> tuple[str, ...]:
    """Return locally available Ollama model IDs from the running daemon or CLI."""
    models = _fetch_ollama_models_via_http()
    if models:
        return models
    return _fetch_ollama_models_via_cli()


def is_known_ollama_model(model_name: str) -> bool:
    """Return whether a bare model name matches a locally available Ollama model."""
    cleaned = model_name.strip().lower()
    if not cleaned:
        return False

    available = {name.lower() for name in get_local_ollama_models()}
    if cleaned in available:
        return True
    if f"{cleaned}:latest" in available:
        return True
    if cleaned.endswith(":latest") and cleaned.removesuffix(":latest") in available:
        return True
    return False


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
    if available_models:
        available = set(available_models)
        for candidate in candidates:
            if candidate in available:
                return candidate
        return next(iter(available_models), None)

    if not candidates:
        return None
    return candidates[0]


def get_supplemental_model_profiles(provider: str) -> Mapping[str, dict[str, Any]]:
    """Return curated model profiles for a provider."""
    return SUPPLEMENTAL_MODEL_PROFILES.get(provider, MappingProxyType({}))


def get_profile_overrides(provider: str) -> Mapping[str, dict[str, Any]]:
    """Return curated profile overrides for an installed provider profile set."""
    return PROFILE_OVERRIDES.get(provider, MappingProxyType({}))


# ---------------------------------------------------------------------------
# Display-name derivation
# ---------------------------------------------------------------------------

_PROVIDER_DISPLAY_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "anthropic": "Anthropic",
        "azure_openai": "Azure OpenAI",
        "baseten": "Baseten",
        "bedrock": "AWS Bedrock",
        "bedrock_converse": "AWS Bedrock",
        "cohere": "Cohere",
        "deepseek": "DeepSeek",
        "fireworks": "Fireworks",
        "google_genai": "Google Gemini",
        "google_vertexai": "Google Vertex AI",
        "groq": "Groq",
        "huggingface": "Hugging Face",
        "ibm": "IBM watsonx",
        "litellm": "LiteLLM",
        "mistralai": "Mistral",
        "nvidia": "NVIDIA NIM",
        "ollama": "Ollama (local)",
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "perplexity": "Perplexity",
        "together": "Together",
        "xai": "xAI",
    }
)


def get_provider_display_name(provider: str) -> str:
    """Return a human-readable label for a provider id (e.g. 'AWS Bedrock').

    Falls back to a title-cased version of the raw id when the provider
    isn't in the curated mapping.
    """
    if provider in _PROVIDER_DISPLAY_NAMES:
        return _PROVIDER_DISPLAY_NAMES[provider]
    return provider.replace("_", " ").title()


@dataclass(frozen=True)
class ModelDisplay:
    """Derived display metadata for a `provider:model` spec.

    Attributes:
        spec: The original `provider:model` string (preserved verbatim).
        display_name: Human-readable label (e.g. 'Claude Sonnet 4.6').
        provider_display: Provider label (e.g. 'AWS Bedrock').
        family: Coarse family tag for grouping ('claude', 'gemini', 'gpt',
            'nova', 'llama', 'mistral', 'ollama', or '' if unknown).
        vendor: Origin vendor when distinct from the API provider — e.g.
            Bedrock-hosted Anthropic models have vendor='anthropic',
            provider='bedrock_converse'.
        supports_thinking: True if the model is known to support native
            extended-thinking / reasoning APIs.
        is_inference_profile: True for Bedrock cross-region inference
            profile IDs (`us.*`, `eu.*`, `apac.*`, ...).
    """

    spec: str
    display_name: str
    provider_display: str
    family: str
    vendor: str
    supports_thinking: bool
    is_inference_profile: bool


_CLAUDE_NAME_RE = re.compile(
    r"claude[-_.]?(opus|sonnet|haiku)[-_.]?(\d+)(?:[-_.](\d+))?",
    re.IGNORECASE,
)
_GEMINI_NAME_RE = re.compile(r"gemini[-_.]?(\d+)(?:[-_.](\d+))?", re.IGNORECASE)
_GPT_NAME_RE = re.compile(r"gpt[-_.]?(\d+)(?:[-_.](\d+))?", re.IGNORECASE)
_NOVA_NAME_RE = re.compile(r"nova[-_.]?(\w+)", re.IGNORECASE)
_LLAMA_NAME_RE = re.compile(r"llama[-_.]?(\d+)(?:[-_.](\d+))?", re.IGNORECASE)


def _strip_bedrock_prefix(model_id: str) -> tuple[str, str, bool]:
    """Split a Bedrock model id into (region_prefix, bare_id, is_profile).

    Returns:
        (region, bare, is_profile) — region is '' when not a cross-region
        profile id, is_profile True when the id has a `us.` / `eu.` /
        `apac.` / etc. prefix.
    """
    lower = model_id.lower()
    for region in BEDROCK_REGION_PREFIXES:
        if lower.startswith(region):
            return (region.rstrip("."), model_id[len(region) :], True)
    return ("", model_id, False)


def _detect_family_and_vendor(model_name: str) -> tuple[str, str]:
    """Return (family, vendor) for a bare model name.

    Vendor is the upstream maker (anthropic/openai/google/meta/mistral/
    amazon/deepseek). Family is a finer grouping suitable for the picker.
    """
    name = model_name.lower()
    # Bedrock vendor prefixes encode the vendor explicitly.
    for vendor_prefix in BEDROCK_VENDOR_PREFIXES:
        if name.startswith(vendor_prefix):
            vendor = vendor_prefix.rstrip(".")
            # Sub-family hint from the remainder.
            tail = name[len(vendor_prefix) :]
            if "claude" in tail:
                return ("claude", vendor)
            if "nova" in tail:
                return ("nova", vendor)
            if "llama" in tail:
                return ("llama", vendor)
            if "mistral" in tail or "pixtral" in tail:
                return ("mistral", vendor)
            if "deepseek" in tail:
                return ("deepseek", vendor)
            return (vendor, vendor)
    if "claude" in name:
        return ("claude", "anthropic")
    if name.startswith(("gemini", "models/gemini")):
        return ("gemini", "google")
    if name.startswith(("gpt-", "chatgpt", "codex-")):
        return ("gpt", "openai")
    if name.startswith(("o1", "o3", "o4")):
        return ("o-series", "openai")
    if "nova" in name:
        return ("nova", "amazon")
    if "llama" in name:
        return ("llama", "meta")
    if "mistral" in name or "pixtral" in name:
        return ("mistral", "mistral")
    if "deepseek" in name:
        return ("deepseek", "deepseek")
    if "qwen" in name:
        return ("qwen", "alibaba")
    if "nemotron" in name:
        return ("nemotron", "nvidia")
    return ("", "")


def supports_native_thinking(model_name: str) -> bool:
    """Return whether a bare model name supports native extended-thinking APIs.

    Mirrors the detection in ``bog_agents.middleware.thinking`` so the CLI
    can show a 'thinking-capable' marker without importing the SDK middleware.
    """
    name = model_name.lower()
    if "claude-3-7" in name or "claude-3.7" in name:
        return True
    if "claude-sonnet-4" in name or "claude-opus-4" in name or "claude-haiku-4" in name:
        return True
    if "gemini-2.5" in name or "gemini-2-5" in name:
        return True
    if "gemini-3" in name:
        return True
    # OpenAI o-series reasoning models.
    return name.startswith(("o1", "o3", "o4"))


def _humanize_claude(bare: str) -> str:
    """Render a Claude bare model id as 'Claude Sonnet 4.6'."""
    m = _CLAUDE_NAME_RE.search(bare)
    if not m:
        return bare
    variant = m.group(1).capitalize()
    major = m.group(2)
    minor = m.group(3)
    label = f"Claude {variant} {major}"
    if minor:
        label += f".{minor}"
    return label


def _humanize_gemini(bare: str) -> str:
    """Render a Gemini bare id as 'Gemini 2.5 Pro'."""
    m = _GEMINI_NAME_RE.search(bare)
    if not m:
        return bare
    major, minor = m.group(1), m.group(2)
    version = f"{major}.{minor}" if minor else major
    tail = bare[m.end() :].lstrip("-_.")
    # Common qualifiers
    qual = ""
    lower_tail = tail.lower()
    for k, v in (("pro", "Pro"), ("flash-lite", "Flash Lite"), ("flash", "Flash")):
        if k in lower_tail:
            qual = v
            break
    if "preview" in lower_tail:
        qual = f"{qual} Preview".strip()
    return f"Gemini {version}{(' ' + qual) if qual else ''}".strip()


def _humanize_gpt(bare: str) -> str:
    """Render an OpenAI GPT bare id as 'GPT-5.4 Mini'."""
    m = _GPT_NAME_RE.search(bare)
    if not m:
        return bare
    major, minor = m.group(1), m.group(2)
    version = f"{major}.{minor}" if minor else major
    tail = bare[m.end() :].lstrip("-_.")
    qual = ""
    if tail:
        if "mini" in tail.lower():
            qual = "Mini"
        elif "nano" in tail.lower():
            qual = "Nano"
        elif "codex" in tail.lower():
            qual = "Codex"
    return f"GPT-{version}{(' ' + qual) if qual else ''}".strip()


def derive_model_display(provider: str, model_name: str) -> ModelDisplay:
    """Derive a `ModelDisplay` for a single `provider:model` pair.

    Pure function — no I/O, safe to call in a tight UI loop.

    Args:
        provider: Provider id (e.g. 'anthropic', 'bedrock_converse').
        model_name: The raw model id as it appears in API calls.

    Returns:
        ModelDisplay with human-readable display_name, family, vendor,
        and capability flags.
    """
    spec = f"{provider}:{model_name}"
    provider_display = get_provider_display_name(provider)

    # Bedrock special-cases: strip region prefix and vendor prefix.
    is_profile = False
    bare = model_name
    if provider in ("bedrock", "bedrock_converse"):
        _region, after_region, is_profile = _strip_bedrock_prefix(model_name)
        # Strip vendor prefix too for the display name.
        for vendor_prefix in BEDROCK_VENDOR_PREFIXES:
            if after_region.lower().startswith(vendor_prefix):
                bare = after_region[len(vendor_prefix) :]
                break
        else:
            bare = after_region

    family, vendor = _detect_family_and_vendor(model_name)

    if family == "claude":
        display = _humanize_claude(bare)
    elif family == "gemini":
        display = _humanize_gemini(bare)
    elif family in ("gpt", "o-series"):
        if family == "o-series":
            # 'o1-mini' -> 'OpenAI o1 Mini'
            parts = bare.split("-")
            head = parts[0]
            tail = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""
            display = f"OpenAI {head}{(' ' + tail) if tail else ''}".strip()
        else:
            display = _humanize_gpt(bare)
    elif family == "nova":
        m = _NOVA_NAME_RE.search(bare)
        tier = m.group(1).capitalize() if m else bare
        display = f"Nova {tier}"
    elif family == "llama":
        m = _LLAMA_NAME_RE.search(bare)
        if m:
            major, minor = m.group(1), m.group(2)
            version = f"{major}.{minor}" if minor else major
            display = f"Llama {version}"
        else:
            display = bare
    elif family == "mistral":
        display = bare.split(":", 1)[0].replace("-", " ").title()
    elif family == "deepseek":
        display = bare.replace("-", " ").title()
    else:
        # Last-resort: title-case the bare name (helps Ollama / unknown).
        display = bare.replace("-", " ").replace("_", " ").title() or model_name

    if is_profile:
        # Tag inference-profile-prefixed Bedrock entries so the picker
        # shows them distinctly from plain regional model ids.
        region, _, _ = _strip_bedrock_prefix(model_name)
        display = f"{display} (Bedrock {region.upper()})"

    return ModelDisplay(
        spec=spec,
        display_name=display,
        provider_display=provider_display,
        family=family,
        vendor=vendor,
        supports_thinking=supports_native_thinking(model_name),
        is_inference_profile=is_profile,
    )


# ---------------------------------------------------------------------------
# Cached model catalog (disk persistence for fast cold-start)
# ---------------------------------------------------------------------------

# Default cache file lives next to user config. Importable as a function so
# tests can monkey-patch ``Path.home`` without polluting module-level state.
_CACHE_FILENAME = "models.cache.json"
_CACHE_FORMAT_VERSION = 1
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # one week — refreshable on demand


def _default_cache_path() -> Path:
    """Return the canonical path to the on-disk model catalog cache."""
    return Path.home() / ".bog-agents" / _CACHE_FILENAME


def load_cached_catalog(
    *, path: Path | None = None
) -> Mapping[str, tuple[str, ...]] | None:
    """Load the persisted model catalog if present and not expired.

    Args:
        path: Override the default cache location (tests).

    Returns:
        A ``{provider: (model, ...)}`` mapping when a fresh cache is on
        disk; ``None`` when there is no cache, the file is malformed, or
        the entry is older than ``_CACHE_TTL_SECONDS``.
    """
    cache_path = path or _default_cache_path()
    try:
        with cache_path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _CACHE_FORMAT_VERSION:
        return None
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    if time.time() - ts > _CACHE_TTL_SECONDS:
        return None

    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return None
    result: dict[str, tuple[str, ...]] = {}
    for provider, models in providers.items():
        if not isinstance(provider, str) or not isinstance(models, list):
            continue
        names = tuple(str(m) for m in models if isinstance(m, str))
        if names:
            result[provider] = names
    return result or None


def save_cached_catalog(
    catalog: Mapping[str, Sequence[str]], *, path: Path | None = None
) -> bool:
    """Persist ``catalog`` to the on-disk cache. Returns success."""
    cache_path = path or _default_cache_path()
    payload: dict[str, Any] = {
        "version": _CACHE_FORMAT_VERSION,
        "ts": time.time(),
        "providers": {p: list(models) for p, models in catalog.items()},
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to tmp then rename so a crash mid-write
        # never leaves a half-written cache file.
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        tmp.replace(cache_path)
    except OSError:
        logger.debug("Failed to persist model catalog cache", exc_info=True)
        return False
    return True


def clear_cached_catalog(*, path: Path | None = None) -> bool:
    """Delete the on-disk cache. Returns whether a file was removed."""
    cache_path = path or _default_cache_path()
    try:
        cache_path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("Failed to delete model catalog cache", exc_info=True)
        return False
    return True
