"""Vault integration for well-known API keys.

Bridges :mod:`bog_agents_cli.vars_store` with :data:`os.environ` so that API
keys stored in the vault are automatically available to model providers,
LangSmith, sandbox integrations, and other services that read ``os.environ``.
"""

from __future__ import annotations

import logging
import os

from bog_agents_cli.model_config import PROVIDER_API_KEY_ENV

logger = logging.getLogger(__name__)

# Provider env-var metadata: (human description, docs URL). Keys NOT covered
# by ``PROVIDER_API_KEY_ENV`` are listed first (non-LLM-provider tooling, plus
# user-facing aliases that downstream libraries also read). Provider keys are
# merged in below so the two registries can never drift out of sync — see
# P0-G in REVIEW.md.
_NON_PROVIDER_KEYS: dict[str, tuple[str, str]] = {
    "LANGSMITH_API_KEY": ("LangSmith API key", "https://smith.langchain.com"),
    "LANGCHAIN_API_KEY": (
        "LangChain/LangSmith API key (alias)",
        "https://smith.langchain.com",
    ),
    "TAVILY_API_KEY": ("Tavily search API key", "https://tavily.com"),
    "DAYTONA_API_KEY": ("Daytona sandbox API key", "https://daytona.io"),
}

# Per-provider metadata for the keys discovered via ``PROVIDER_API_KEY_ENV``.
# When a provider lists multiple env vars (only one today, but easily
# extended) we keep the entry keyed by env-var so users find what they expect.
_PROVIDER_KEY_METADATA: dict[str, tuple[str, str]] = {
    "ANTHROPIC_API_KEY": ("Anthropic API key", "https://console.anthropic.com"),
    "AZURE_OPENAI_API_KEY": ("Azure OpenAI API key", "https://azure.microsoft.com/en-us/products/ai-services/openai-service"),
    "BASETEN_API_KEY": ("Baseten API key", "https://www.baseten.co"),
    "AWS_ACCESS_KEY_ID": ("AWS access key (Bedrock)", "https://aws.amazon.com/bedrock"),
    "COHERE_API_KEY": ("Cohere API key", "https://dashboard.cohere.com"),
    "DEEPSEEK_API_KEY": ("DeepSeek API key", "https://platform.deepseek.com"),
    "FIREWORKS_API_KEY": ("Fireworks AI API key", "https://fireworks.ai"),
    "GOOGLE_API_KEY": ("Google AI API key", "https://aistudio.google.com"),
    "GOOGLE_CLOUD_PROJECT": ("Google Cloud project (Vertex AI)", "https://cloud.google.com/vertex-ai"),
    "GROQ_API_KEY": ("Groq API key", "https://console.groq.com"),
    "HUGGINGFACEHUB_API_TOKEN": ("Hugging Face Hub token", "https://huggingface.co"),
    "WATSONX_APIKEY": ("IBM watsonx.ai API key", "https://www.ibm.com/watsonx"),
    "LITELLM_API_KEY": ("LiteLLM proxy API key", "https://litellm.ai"),
    "MISTRAL_API_KEY": ("Mistral AI API key", "https://console.mistral.ai"),
    "NVIDIA_API_KEY": ("NVIDIA NIM API key", "https://build.nvidia.com"),
    "OPENAI_API_KEY": ("OpenAI API key", "https://platform.openai.com"),
    "OPENROUTER_API_KEY": ("OpenRouter API key", "https://openrouter.ai"),
    "PPLX_API_KEY": ("Perplexity API key", "https://www.perplexity.ai/settings/api"),
    "TOGETHER_API_KEY": ("Together AI API key", "https://api.together.xyz"),
    "XAI_API_KEY": ("xAI API key", "https://console.x.ai"),
}

# Common alias users will type from memory but that the SDK does NOT read.
# Stored in the vault under the canonical name (e.g. ``PPLX_API_KEY``) when
# the user sets the alias.
_PROVIDER_KEY_ALIASES: dict[str, str] = {
    "PERPLEXITY_API_KEY": "PPLX_API_KEY",
}


def _build_well_known_api_keys() -> dict[str, tuple[str, str]]:
    """Derive the well-known API-key registry from the source maps.

    Combines ``PROVIDER_API_KEY_ENV`` and the manual metadata maps
    above. A future typo (provider added to one map but not the
    other) raises at import time rather than slipping through as a
    silent miss.

    Raises:
        RuntimeError: When a provider env-var listed in
            ``PROVIDER_API_KEY_ENV`` has no matching entry in
            ``_PROVIDER_KEY_METADATA``.
    """
    out: dict[str, tuple[str, str]] = dict(_NON_PROVIDER_KEYS)
    for env_var in PROVIDER_API_KEY_ENV.values():
        meta = _PROVIDER_KEY_METADATA.get(env_var)
        if meta is None:
            # If a new provider lands in model_config.py without metadata
            # here, fail loudly rather than silently dropping vault support.
            msg = (
                f"api_keys.py is missing metadata for env-var {env_var!r}. "
                "Add an entry to _PROVIDER_KEY_METADATA when adding a new provider."
            )
            raise RuntimeError(msg)
        out[env_var] = meta
    return out


WELL_KNOWN_API_KEYS: dict[str, tuple[str, str]] = _build_well_known_api_keys()
"""Public registry of env-var → (description, docs URL).

Derived from ``model_config.PROVIDER_API_KEY_ENV`` so a new provider is
automatically picked up by the vault and the ``/vars`` UI. To add a new
provider:

1. Add it to ``PROVIDER_API_KEY_ENV`` in ``model_config.py``.
2. Add a description + URL entry to ``_PROVIDER_KEY_METADATA`` here.

A test asserts the two stay in sync.
"""


def vault_key_name(env_var: str) -> str:
    """Return the vars_store key name for an env var.

    Uses the same name as the environment variable for simplicity.

    Args:
        env_var: Environment variable name (e.g. ``"OPENAI_API_KEY"``).

    Returns:
        The vars_store key name (identical to the env-var name).
    """
    return env_var


def inject_vault_keys_into_env() -> list[str]:
    """Read all well-known API keys from vault and inject missing ones into os.environ.

    Only keys that are absent from ``os.environ`` are written.  Keys that are
    already set (e.g. via a ``.env`` file or the shell) are left untouched.

    This function is intentionally fast and silent on errors — it is called at
    startup before the UI is initialised.

    Returns:
        List of env-var names that were injected from the vault.
    """
    try:
        from bog_agents_cli.vars_store import get_var
    except Exception:
        logger.debug("vars_store unavailable; skipping vault key injection")
        return []

    injected: list[str] = []
    for env_var in WELL_KNOWN_API_KEYS:
        if os.environ.get(env_var):
            continue
        try:
            value = get_var(env_var)
        except Exception:
            logger.debug("Could not read vault key %r", env_var, exc_info=True)
            continue
        if value:
            os.environ[env_var] = value
            injected.append(env_var)

    # Resolve aliases — e.g. ``PERPLEXITY_API_KEY`` (user-facing name) ->
    # ``PPLX_API_KEY`` (what langchain-perplexity reads). Honor both stored
    # vault entries and an environment alias the user may have exported.
    for alias, canonical in _PROVIDER_KEY_ALIASES.items():
        if os.environ.get(canonical):
            continue
        # Try alias env var first, then alias vault key.
        value = os.environ.get(alias)
        if not value:
            try:
                value = get_var(alias)
            except Exception:
                logger.debug("Could not read alias vault key %r", alias, exc_info=True)
                value = None
        if value:
            os.environ[canonical] = value
            if canonical not in injected:
                injected.append(canonical)
    return injected


def get_api_key(env_var: str) -> str | None:
    """Get an API key, checking os.environ first then the vault.

    When the key is found in the vault but not in ``os.environ``, it is
    injected into ``os.environ`` so that downstream library code (which reads
    ``os.environ`` directly) also picks it up.

    Args:
        env_var: Environment variable name (e.g. ``"OPENAI_API_KEY"``).

    Returns:
        The key value, or ``None`` if not found in either location.
    """
    value = os.environ.get(env_var) or None
    if value:
        return value

    try:
        from bog_agents_cli.vars_store import get_var
    except Exception:
        logger.debug("vars_store unavailable for key lookup %r", env_var)
        return None

    try:
        value = get_var(env_var)
    except Exception:
        logger.debug("Could not read vault key %r", env_var, exc_info=True)
        return None

    if value:
        os.environ[env_var] = value
    return value or None


def save_api_key(env_var: str, value: str) -> None:
    """Save an API key to the vault and set it in os.environ.

    Args:
        env_var: Environment variable name (e.g. ``"OPENAI_API_KEY"``).
        value: The API key value to store.
    """
    from bog_agents_cli.vars_store import set_var

    set_var(env_var, value)
    os.environ[env_var] = value
