"""Vault integration for well-known API keys.

Bridges :mod:`bog_agents_cli.vars_store` with :data:`os.environ` so that API
keys stored in the vault are automatically available to model providers,
LangSmith, sandbox integrations, and other services that read ``os.environ``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Map of env-var name -> (human description, docs URL)
WELL_KNOWN_API_KEYS: dict[str, tuple[str, str]] = {
    "LANGSMITH_API_KEY": ("LangSmith API key", "https://smith.langchain.com"),
    "ANTHROPIC_API_KEY": ("Anthropic API key", "https://console.anthropic.com"),
    "OPENAI_API_KEY": ("OpenAI API key", "https://platform.openai.com"),
    "GOOGLE_API_KEY": ("Google AI API key", "https://aistudio.google.com"),
    "GROQ_API_KEY": ("Groq API key", "https://console.groq.com"),
    "MISTRAL_API_KEY": ("Mistral AI API key", "https://console.mistral.ai"),
    "COHERE_API_KEY": ("Cohere API key", "https://dashboard.cohere.com"),
    "NVIDIA_API_KEY": ("NVIDIA NIM API key", "https://build.nvidia.com"),
    "FIREWORKS_API_KEY": ("Fireworks AI API key", "https://fireworks.ai"),
    "DEEPSEEK_API_KEY": ("DeepSeek API key", "https://platform.deepseek.com"),
    "XAI_API_KEY": ("xAI API key", "https://console.x.ai"),
    "OPENROUTER_API_KEY": ("OpenRouter API key", "https://openrouter.ai"),
    "TAVILY_API_KEY": ("Tavily search API key", "https://tavily.com"),
    "LANGCHAIN_API_KEY": ("LangChain/LangSmith API key (alias)", "https://smith.langchain.com"),
    "DAYTONA_API_KEY": ("Daytona API key", "https://daytona.io"),
}


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
