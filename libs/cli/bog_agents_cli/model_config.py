"""Model configuration management.

Handles loading and saving model configuration from TOML files, providing a
structured way to define available models and providers.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

import tomli_w

if TYPE_CHECKING:
    from collections.abc import Mapping

from bog_agents_cli._debug import configure_debug_logging
from bog_agents_cli.provider_catalog import (
    clear_cached_catalog,
    clear_provider_catalog_caches,
    get_local_ollama_models,
    get_profile_overrides as get_curated_profile_overrides,
    get_supplemental_model_profiles,
    load_cached_catalog,
    save_cached_catalog,
)

logger = logging.getLogger(__name__)
configure_debug_logging(logger)


class ModelConfigError(Exception):
    """Raised when model configuration or creation fails."""


@dataclass(frozen=True)
class ModelSpec:
    """A model specification in `provider:model` format.

    Examples:
        >>> spec = ModelSpec.parse("anthropic:claude-sonnet-4-5")
        >>> spec.provider
        'anthropic'
        >>> spec.model
        'claude-sonnet-4-5'
        >>> str(spec)
        'anthropic:claude-sonnet-4-5'
    """

    provider: str
    """The provider name (e.g., `'anthropic'`, `'openai'`)."""

    model: str
    """The model identifier (e.g., `'claude-sonnet-4-5'`, `'gpt-4o'`)."""

    def __post_init__(self) -> None:
        """Validate the model spec after initialization.

        Raises:
            ValueError: If provider or model is empty.
        """
        if not self.provider:
            msg = "Provider cannot be empty"
            raise ValueError(msg)
        if not self.model:
            msg = "Model cannot be empty"
            raise ValueError(msg)

    @classmethod
    def parse(cls, spec: str) -> ModelSpec:
        """Parse a model specification string.

        Args:
            spec: Model specification in `'provider:model'` format.

        Returns:
            Parsed ModelSpec instance.

        Raises:
            ValueError: If the spec is not in valid `'provider:model'` format.
        """
        if ":" not in spec:
            msg = (
                f"Invalid model spec '{spec}': must be in provider:model format "
                "(e.g., 'anthropic:claude-sonnet-4-5')"
            )
            raise ValueError(msg)
        provider, model = spec.split(":", 1)
        return cls(provider=provider, model=model)

    @classmethod
    def try_parse(cls, spec: str) -> ModelSpec | None:
        """Non-raising variant of `parse`.

        Args:
            spec: Model specification in `provider:model` format.

        Returns:
            Parsed `ModelSpec`, or `None` when *spec* is not valid.
        """
        try:
            return cls.parse(spec)
        except ValueError:
            return None

    def __str__(self) -> str:
        """Return the model spec as a string in `provider:model` format."""
        return f"{self.provider}:{self.model}"


class ModelProfileEntry(TypedDict):
    """Profile data for a model with override tracking."""

    profile: dict[str, Any]
    """Merged profile dict (upstream defaults + config.toml overrides).

    Keys vary by provider (e.g., `max_input_tokens`, `tool_calling`).
    """

    overridden_keys: frozenset[str]
    """Keys in `profile` whose values came from config.toml rather than the
    upstream provider package."""


class ProviderConfig(TypedDict, total=False):
    """Configuration for a model provider.

    The optional `class_path` field allows bypassing `init_chat_model` entirely
    and instantiating an arbitrary `BaseChatModel` subclass via importlib.

    !!! warning

        Setting `class_path` executes arbitrary Python code from the user's
        config file. This has the same trust model as `pyproject.toml` build
        scripts — the user controls their own machine.
    """

    models: list[str]
    """List of model identifiers available from this provider."""

    api_key_env: str
    """Environment variable name containing the API key."""

    base_url: str
    """Custom base URL."""

    # Level 2: arbitrary BaseChatModel classes

    class_path: str
    """Fully-qualified Python class in `module.path:ClassName` format.

    When set, `create_model` imports this class and instantiates it directly
    instead of calling `init_chat_model`.
    """

    params: dict[str, Any]
    """Extra keyword arguments forwarded to the model constructor.

    Flat keys (e.g., `temperature = 0`) are provider-wide defaults applied to
    every model from this provider. Model-keyed sub-tables (e.g.,
    `[params."qwen3:4b"]`) override individual values for that model only;
    the merge is shallow (model wins on conflict).
    """

    profile: dict[str, Any]
    """Overrides merged into the model's runtime profile dict.

    Flat keys (e.g., `max_input_tokens = 4096`) are provider-wide defaults.
    Model-keyed sub-tables (e.g., `[profile."claude-sonnet-4-5"]`) override
    individual values for that model only; the merge is shallow.
    """

    credential_check: str
    """Credential verification strategy for this provider.

    Supported values:
    - `'thorough'` (default): Resolves credentials via boto3 and freezes
      them to verify the access key is non-empty. Catches expired SSO
      tokens and misconfigured profiles.
    - `'boto3'`: Delegates entirely to boto3's credential chain. Faster
      but does not validate that resolved credentials are still valid.
    - `'files'`: Legacy file-existence checks (env vars, ~/.aws/ files).
      Fastest but cannot detect expired credentials.
    """

    auth_mode: str
    """Bedrock-only — which credential source(s) to use, in priority order.

    Supported values:
    - `'auto'` (default): Try every source boto3 knows about. On
      TokenRetrievalError from a configured-but-expired SSO session,
      automatically retry with a static-credentials-only session so
      fresh keys in `~/.aws/credentials` work even when the SSO config
      in `~/.aws/config` is stale.
    - `'sso'`: Force the SSO path. Fail loudly if the session is expired.
    - `'static'`: Use only `~/.aws/credentials` (or AWS_ACCESS_KEY_ID /
      AWS_SECRET_ACCESS_KEY env vars). Never touches SSO config — useful
      when you have working static creds and want to ignore the SSO
      session that boto3 would otherwise prefer.
    - `'profile'`: Use the named AWS profile from the `aws_profile`
      setting below. Both SSO-backed and static-creds profiles are honored.
    - `'iam'`: Force IAM instance/role credentials (EC2/ECS/Lambda).
    """

    aws_profile: str
    """Bedrock-only — the named AWS profile when `auth_mode='profile'`.

    Distinct from the ``profile`` key above (which is the model-runtime
    profile-overrides dict). Keep these separate to avoid a TOML key
    collision; the boto3 profile lives at ``aws_profile`` in config.
    """


DEFAULT_CONFIG_DIR = Path.home() / ".bog-agents"
"""Directory for user-level Bog Agents configuration (`~/.bog-agents`)."""

DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
"""Path to the user's model configuration file (`~/.bog-agents/config.toml`)."""

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",
    "bedrock_converse": "AWS_ACCESS_KEY_ID",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_CLOUD_PROJECT",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "ibm": "WATSONX_APIKEY",
    "litellm": "LITELLM_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PPLX_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
}
"""Well-known providers mapped to the env var that holds their API key.

Used by `has_provider_credentials` to verify credentials *before* model
creation, so the UI can show a warning icon and a specific error message
(e.g., "ANTHROPIC_API_KEY not set") instead of letting the provider fail at call
time.

Providers not listed here fall through to the config-file check or the langchain
registry fallback.
"""


# Module-level caches — cleared by `clear_caches()`.
_available_models_cache: dict[str, list[str]] | None = None
_builtin_providers_cache: dict[str, Any] | None = None
_default_config_cache: ModelConfig | None = None
_profiles_cache: Mapping[str, ModelProfileEntry] | None = None
_profiles_override_cache: tuple[int, Mapping[str, ModelProfileEntry]] | None = None


def clear_caches() -> None:
    """Reset module-level caches so the next call recomputes from scratch.

    Intended for tests and for the `/reload` command.
    """
    global _available_models_cache, _builtin_providers_cache, _default_config_cache, _profiles_cache, _profiles_override_cache  # noqa: PLW0603  # Module-level caches require global statement
    _available_models_cache = None
    _builtin_providers_cache = None
    _default_config_cache = None
    _profiles_cache = None
    _profiles_override_cache = None
    clear_provider_catalog_caches()
    invalidate_thread_config_cache()


def _get_builtin_providers() -> dict[str, Any]:
    """Return langchain's built-in provider registry.

    Tries the newer `_BUILTIN_PROVIDERS` name first, then falls back to
    the legacy `_SUPPORTED_PROVIDERS` for older langchain versions.

    Results are cached after the first call; use `clear_caches()` to reset.

    Returns:
        The provider registry dict from `langchain.chat_models.base`.
    """
    global _builtin_providers_cache  # noqa: PLW0603  # Module-level cache requires global statement
    if _builtin_providers_cache is not None:
        return _builtin_providers_cache

    # Deferred: langchain.chat_models pulls in heavy provider registry,
    # only needed when resolving provider names for model config.
    from langchain.chat_models import base

    registry: dict[str, Any] | None = getattr(base, "_BUILTIN_PROVIDERS", None)
    if registry is None:
        registry = getattr(base, "_SUPPORTED_PROVIDERS", None)
    _builtin_providers_cache = registry if registry is not None else {}
    return _builtin_providers_cache


def _get_provider_profile_modules() -> list[tuple[str, str]]:
    """Build a `(provider, profile_module)` list from langchain's provider registry.

    Reads the built-in provider registry from `langchain.chat_models.base`
    to discover every provider that `init_chat_model` knows about, then derives
    the `<package>.data._profiles` module path for each.

    Returns:
        List of `(provider_name, profile_module_path)` tuples.
    """
    providers = _get_builtin_providers()

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for provider_name, (module_path, *_rest) in providers.items():
        package_root = module_path.split(".", maxsplit=1)[0]
        profile_module = f"{package_root}.data._profiles"
        key = (provider_name, profile_module)
        if key not in seen:
            seen.add(key)
            result.append((provider_name, profile_module))

    return result


def _provider_package_is_installed(provider: str) -> bool:
    """Return whether the provider's LangChain package is importable."""
    registry_entry = _get_builtin_providers().get(provider)
    if registry_entry is None:
        return False
    package_name = registry_entry[0]
    try:
        return importlib.util.find_spec(package_name) is not None
    except ModuleNotFoundError:
        return False


def _load_provider_profiles(module_path: str) -> dict[str, Any]:
    """Load `_PROFILES` from a provider's data module.

    Locating the package on disk with `importlib.util.find_spec` and load *only*
    the `_profiles.py` file via `spec_from_file_location`.

    Args:
        module_path: Dotted module path (e.g., `"langchain_openai.data._profiles"`).

    Returns:
        The `_PROFILES` dictionary from the module, or an empty dict if
            the module has no such attribute.

    Raises:
        ImportError: If the package is not installed or the profile module
            cannot be found on disk.
    """
    parts = module_path.split(".")
    package_root = parts[0]

    spec = importlib.util.find_spec(package_root)
    if spec is None:
        msg = f"Package {package_root} is not installed"
        raise ImportError(msg)

    # Determine the package directory from the spec.
    if spec.origin:
        package_dir = Path(spec.origin).parent
    elif spec.submodule_search_locations:
        package_dir = Path(next(iter(spec.submodule_search_locations)))
    else:
        msg = f"Cannot determine location for {package_root}"
        raise ImportError(msg)

    # Build the path to the target file (e.g., data/_profiles.py).
    relative_parts = parts[1:]  # ["data", "_profiles"]
    profiles_path = package_dir.joinpath(
        *relative_parts[:-1], f"{relative_parts[-1]}.py"
    )

    if not profiles_path.exists():
        msg = f"Profile module not found: {profiles_path}"
        raise ImportError(msg)

    file_spec = importlib.util.spec_from_file_location(module_path, profiles_path)
    if file_spec is None or file_spec.loader is None:
        msg = f"Could not create module spec for {profiles_path}"
        raise ImportError(msg)

    module = importlib.util.module_from_spec(file_spec)
    file_spec.loader.exec_module(module)
    return getattr(module, "_PROFILES", {})


def _profile_module_from_class_path(class_path: str) -> str | None:
    """Derive the profile module path from a `class_path` config value.

    Args:
        class_path: Fully-qualified class in `module.path:ClassName` format.

    Returns:
        Dotted module path like `langchain_baseten.data._profiles`, or None
            if `class_path` is malformed.
    """
    if ":" not in class_path:
        return None
    module_part, _ = class_path.split(":", 1)
    package_root = module_part.split(".", maxsplit=1)[0]
    if not package_root:
        return None
    return f"{package_root}.data._profiles"


def get_available_models() -> dict[str, list[str]]:
    """Get available models dynamically from installed LangChain provider packages.

    Imports model profiles from each provider package and extracts model names.

    Results are cached after the first call; use `clear_caches()` to reset.

    Returns:
        Dictionary mapping provider names to lists of model identifiers.
            Includes providers from the langchain registry, config-file
            providers with explicit model lists, and `class_path` providers
            whose packages expose a `_profiles` module.
    """
    global _available_models_cache  # noqa: PLW0603  # Module-level cache requires global statement
    if _available_models_cache is not None:
        return _available_models_cache

    available: dict[str, list[str]] = {}

    # Try to load from langchain provider profile data.
    # Build the list dynamically from langchain's supported-provider registry
    # so new providers are picked up automatically when langchain adds them.
    provider_modules = _get_provider_profile_modules()
    registry_providers: set[str] = set()

    for provider, module_path in provider_modules:
        registry_providers.add(provider)
        try:
            profiles = _load_provider_profiles(module_path)
        except ImportError:
            logger.debug(
                "Could not import profiles from %s (package may not be installed)",
                module_path,
            )
            continue
        except Exception:
            logger.warning(
                "Failed to load profiles from %s, skipping provider '%s'",
                module_path,
                provider,
                exc_info=True,
            )
            continue

        # Filter to models that support tool calling and text I/O.
        models = [
            name
            for name, profile in profiles.items()
            if profile.get("tool_calling", False)
            and profile.get("text_inputs", True) is not False
            and profile.get("text_outputs", True) is not False
        ]

        models.sort()
        if models:
            available[provider] = models

    # Merge in models from config file (custom providers like ollama, fireworks)
    config = ModelConfig.load()
    for provider_name, provider_config in config.providers.items():
        config_models = list(provider_config.get("models", []))

        # For class_path providers not in the built-in registry, auto-discover
        # models from the package's _profiles.py when no explicit models list.
        if (
            not config_models
            and provider_name not in registry_providers
            and provider_name not in available
        ):
            class_path = provider_config.get("class_path", "")
            profile_module = _profile_module_from_class_path(class_path)
            if profile_module:
                try:
                    profiles = _load_provider_profiles(profile_module)
                except ImportError:
                    logger.debug(
                        "Could not import profiles from %s for class_path "
                        "provider '%s' (package may not be installed)",
                        profile_module,
                        provider_name,
                    )
                except Exception:
                    logger.warning(
                        "Failed to load profiles from %s for class_path provider '%s'",
                        profile_module,
                        provider_name,
                        exc_info=True,
                    )
                else:
                    config_models = sorted(
                        name
                        for name, profile in profiles.items()
                        if profile.get("tool_calling", False)
                        and profile.get("text_inputs", True) is not False
                        and profile.get("text_outputs", True) is not False
                    )

        if provider_name not in available:
            if config_models:
                available[provider_name] = config_models
        else:
            # Append any config models not already discovered
            existing = set(available[provider_name])
            for model in config_models:
                if model not in existing:
                    available[provider_name].append(model)

    # Merge curated model profiles for newer vendor releases that may not be in
    # the installed LangChain snapshot yet.
    for provider in tuple(_get_builtin_providers()):
        if not _provider_package_is_installed(provider):
            continue

        supplemental = get_supplemental_model_profiles(provider)
        if not supplemental:
            continue

        provider_models = available.setdefault(provider, [])
        existing = set(provider_models)
        for model_name, profile in supplemental.items():
            if model_name in existing:
                continue
            if not profile.get("tool_calling", False):
                continue
            if profile.get("text_inputs", True) is False:
                continue
            if profile.get("text_outputs", True) is False:
                continue
            provider_models.append(model_name)
            existing.add(model_name)
        provider_models.sort()

    # Merge in live local Ollama models so the catalog reflects what the user
    # can actually run on this machine, not just static profile snapshots.
    if _provider_package_is_installed("ollama"):
        local_ollama_models = get_local_ollama_models()
        if local_ollama_models:
            provider_models = available.setdefault("ollama", [])
            existing = set(provider_models)
            for model_name in local_ollama_models:
                if model_name in existing:
                    continue
                provider_models.append(model_name)
                existing.add(model_name)

    _available_models_cache = available
    # Persist a snapshot to disk so the next cold start can paint the
    # picker instantly while a background refresh runs. Best-effort —
    # a failed save (read-only dir, etc.) is just a missed optimization.
    try:
        save_cached_catalog(available)
    except Exception:
        logger.debug("Failed to persist model catalog cache", exc_info=True)
    return available


def refresh_available_models() -> dict[str, list[str]]:
    """Force a re-scan of installed provider packages and live Ollama list.

    Clears the in-memory cache plus any provider-catalog caches (so the
    Ollama HTTP probe re-runs) and re-derives the model list. The fresh
    list is also persisted to ``~/.bog-agents/models.cache.json`` for
    the next cold start.

    Returns:
        The refreshed ``{provider: [model, ...]}`` catalog.
    """
    global _available_models_cache  # noqa: PLW0603
    _available_models_cache = None
    clear_provider_catalog_caches()
    try:
        clear_cached_catalog()
    except Exception:
        logger.debug("Failed to delete model catalog cache", exc_info=True)
    return get_available_models()


def get_cached_available_models() -> dict[str, list[str]] | None:
    """Return the disk-cached catalog (if present) without a live scan.

    Useful at cold-start: the CLI can paint the picker from the cached
    snapshot in microseconds, then call ``refresh_available_models()``
    in the background to update.
    """
    cached = load_cached_catalog()
    if cached is None:
        return None
    return {provider: list(models) for provider, models in cached.items()}


def _build_entry(
    base: dict[str, Any],
    overrides: dict[str, Any],
    cli_override: dict[str, Any] | None,
) -> ModelProfileEntry:
    """Build a profile entry by merging base, overrides, and CLI override.

    Args:
        base: Upstream profile dict (empty for config-only models).
        overrides: `config.toml` profile overrides.
        cli_override: Extra fields from `--profile-override`.

    Returns:
        Profile entry with merged data and override tracking.
    """
    merged = {**base, **overrides}
    overridden_keys = set(overrides)
    if cli_override:
        merged = {**merged, **cli_override}
        overridden_keys |= set(cli_override)
    return ModelProfileEntry(
        profile=merged,
        overridden_keys=frozenset(overridden_keys),
    )


def get_model_profiles(
    *,
    cli_override: dict[str, Any] | None = None,
) -> Mapping[str, ModelProfileEntry]:
    """Load upstream profiles merged with config.toml overrides.

    Keyed by `provider:model` spec string. Each entry contains the
    merged profile dict and the set of keys overridden by config.toml.

    Unlike `get_available_models()`, this includes all models from upstream
    profiles regardless of capability filters (tool calling, text I/O).

    Results are cached; use `clear_caches()` to reset. When `cli_override` is
    provided the result is stored in a single-slot cache keyed by
    `id(cli_override)`. This relies on the caller retaining the same dict
    object for the session (the CLI stores it once on the app instance);
    passing a different dict with the same contents will bypass the cache
    and overwrite the previous entry.

    Args:
        cli_override: Extra profile fields from `--profile-override`.

            When provided, these are merged on top of every profile entry
            (after upstream + config.toml) and their keys are added to
            `overridden_keys`.

    Returns:
        Read-only mapping of spec strings to profile entries.
    """
    global _profiles_cache, _profiles_override_cache  # noqa: PLW0603  # Module-level caches require global statement
    if cli_override is None and _profiles_cache is not None:
        return _profiles_cache
    if cli_override is not None and _profiles_override_cache is not None:
        cached_id, cached_result = _profiles_override_cache
        if cached_id == id(cli_override):
            return cached_result

    result: dict[str, ModelProfileEntry] = {}
    config = ModelConfig.load()

    # Collect upstream profiles from provider packages.
    seen_specs: set[str] = set()
    provider_modules = _get_provider_profile_modules()
    registry_providers: set[str] = set()
    for provider, module_path in provider_modules:
        registry_providers.add(provider)
        try:
            profiles = _load_provider_profiles(module_path)
        except ImportError:
            logger.debug(
                "Could not import profiles from %s for provider '%s'",
                module_path,
                provider,
            )
            continue
        except Exception:
            logger.warning(
                "Failed to load profiles from %s for provider '%s'",
                module_path,
                provider,
                exc_info=True,
            )
            continue

        for model_name, upstream_profile in profiles.items():
            spec = f"{provider}:{model_name}"
            seen_specs.add(spec)
            curated_overrides = get_curated_profile_overrides(provider).get(
                model_name, {}
            )
            overrides = config.get_profile_overrides(provider, model_name=model_name)
            result[spec] = _build_entry(
                {**upstream_profile, **curated_overrides},
                overrides,
                cli_override,
            )

    # Add config-only models and class_path provider profiles.
    for provider_name, provider_config in config.providers.items():
        # For class_path providers not in the built-in registry, load
        # upstream profiles from the package's _profiles.py.
        if provider_name not in registry_providers:
            class_path = provider_config.get("class_path", "")
            profile_module = _profile_module_from_class_path(class_path)
            if profile_module:
                try:
                    pkg_profiles = _load_provider_profiles(profile_module)
                except ImportError:
                    logger.debug(
                        "Could not import profiles from %s for class_path "
                        "provider '%s' (package may not be installed)",
                        profile_module,
                        provider_name,
                    )
                except Exception:
                    logger.warning(
                        "Failed to load profiles from %s for class_path provider '%s'",
                        profile_module,
                        provider_name,
                        exc_info=True,
                    )
                else:
                    for model_name, upstream_profile in pkg_profiles.items():
                        spec = f"{provider_name}:{model_name}"
                        seen_specs.add(spec)
                        overrides = config.get_profile_overrides(
                            provider_name, model_name=model_name
                        )
                        result[spec] = _build_entry(
                            upstream_profile, overrides, cli_override
                        )

        config_models = provider_config.get("models", [])
        for model_name in config_models:
            spec = f"{provider_name}:{model_name}"
            if spec not in seen_specs:
                overrides = config.get_profile_overrides(
                    provider_name, model_name=model_name
                )
                result[spec] = _build_entry({}, overrides, cli_override)

    for provider in tuple(_get_builtin_providers()):
        if not _provider_package_is_installed(provider):
            continue
        for model_name, base_profile in get_supplemental_model_profiles(
            provider
        ).items():
            spec = f"{provider}:{model_name}"
            if spec in seen_specs:
                continue
            seen_specs.add(spec)
            overrides = config.get_profile_overrides(provider, model_name=model_name)
            result[spec] = _build_entry(base_profile, overrides, cli_override)

    frozen = MappingProxyType(result)
    if cli_override is None:
        _profiles_cache = frozen
    else:
        _profiles_override_cache = (id(cli_override), frozen)
    return frozen


def has_provider_credentials(provider: str) -> bool | None:
    """Check if credentials are available for a provider.

    Resolution order:

    1. Bedrock — uses the AWS credential chain or a Bedrock API key
        (AWS_BEARER_TOKEN_BEDROCK); resolved by `_has_bedrock_credentials()`
        ahead of the config-file check so a user-defined `api_key_env` can't
        mask a valid profile/SSO/bearer-token credential.
    2. Config-file providers (`config.toml`) — user overrides (e.g., custom
        `api_key_env` or `base_url`) are respected.
    3. Hardcoded `PROVIDER_API_KEY_ENV` mapping (anthropic, openai, etc.).
    4. For any other provider (e.g., third-party langchain provider
        packages), credential status is unknown — the provider itself will
        report auth failures at model-creation time.

    Args:
        provider: Provider name.

    Returns:
        True if credentials are confirmed available, False if confirmed
            missing, or None if credential status cannot be determined.
    """
    # Bedrock uses the AWS credential chain (SSO, profiles, instance roles)
    # OR a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) — checking a single
    # `api_key_env` is insufficient and would wrongly report "no creds" for
    # a user authenticating purely via the bearer token or a profile. This
    # takes priority over the config-file `api_key_env` check below so a
    # `[models.providers.bedrock]` block doesn't mask the AWS-chain probe.
    if provider in ("bedrock", "bedrock_converse"):
        return _has_bedrock_credentials()

    # Config-file providers take priority when api_key_env is specified.
    config = ModelConfig.load()
    if config.providers.get(provider):
        result = config.has_credentials(provider)
        if result is not None:
            return result
        # No api_key_env in config — fall through to hardcoded map.

    # Fall back to hardcoded well-known providers.
    env_var = PROVIDER_API_KEY_ENV.get(provider)
    if env_var:
        return bool(os.environ.get(env_var))

    # Provider not found in config or hardcoded map — credential status is
    # unknown. The provider itself will report auth failures at
    # model-creation time.
    logger.debug(
        "No credential information for provider '%s'; deferring auth to provider",
        provider,
    )
    return None


def _has_bedrock_credentials() -> bool:
    """Check if AWS credentials are available for Bedrock.

    The check strategy is controlled by the `credential_check` setting in
    `[models.providers.bedrock]` of the user's config.toml:

    - `'thorough'` (default): Resolves credentials via boto3, then freezes
      them to verify the access key is present. Catches expired SSO tokens
      and misconfigured profiles without making a network call.
    - `'boto3'`: Asks boto3 if *any* credential source exists. Faster but
      won't detect expired tokens until call time.
    - `'files'`: Fast file/env-var existence checks only.

    Returns:
        True if credentials are detected per the chosen strategy.
    """
    # A Bedrock API key (bearer token) is sufficient on its own regardless of
    # the configured check mode — short-circuit before any SigV4 probe.
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        return True

    config = ModelConfig.load()
    bedrock_cfg = config.providers.get("bedrock_converse") or config.providers.get(
        "bedrock", {}
    )
    mode = bedrock_cfg.get("credential_check", "thorough")

    if mode == "thorough":
        return _check_bedrock_thorough()
    if mode == "boto3":
        return _check_bedrock_boto3()
    if mode == "files":
        return _check_bedrock_files()

    logger.warning(
        "Unknown bedrock credential_check mode '%s'; falling back to 'thorough'",
        mode,
    )
    return _check_bedrock_thorough()


# Negative-cache for the bedrock credential probe. The langchain
# auto-detect loop and the model resolver each call _has_bedrock_credentials
# many times in succession; without caching, a single expired SSO session
# produces 30+ identical TokenRetrievalError tracebacks in the log file
# (issue #53). The cache key is the boto3 credential method (env vs
# profile vs sso) — when that changes mid-process (extremely rare) we'd
# stay stale for at most one probe, which is acceptable.
_BEDROCK_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def _bedrock_auth_mode() -> tuple[str, str]:
    """Resolve the Bedrock auth-mode + profile name from config / env.

    Precedence (highest first):
      1. ``BOG_AGENTS_BEDROCK_AUTH_MODE`` env var
      2. ``[models.providers.bedrock] auth_mode`` in config.toml
      3. ``[models.providers.bedrock_converse] auth_mode`` (legacy)
      4. ``"auto"`` default

    The profile name has the same precedence chain via the env var
    ``BOG_AGENTS_BEDROCK_PROFILE`` and the ``profile`` config key, then
    finally ``AWS_PROFILE`` from the environment as a sensible default.

    Returns:
        (mode, profile_name) — mode is one of the documented strings;
        profile_name is the empty string when not configured.
    """
    env_mode = os.environ.get("BOG_AGENTS_BEDROCK_AUTH_MODE", "").strip().lower()
    if env_mode:
        mode = env_mode
    else:
        try:
            cfg = ModelConfig.load()
        except (OSError, ValueError):
            cfg = None
        bedrock_cfg: Mapping[str, Any] = {}
        if cfg is not None:
            bedrock_cfg = cfg.providers.get("bedrock_converse") or cfg.providers.get(
                "bedrock", {}
            )
        mode = (bedrock_cfg.get("auth_mode") or "auto").strip().lower()

    env_profile = os.environ.get("BOG_AGENTS_BEDROCK_PROFILE", "").strip()
    if env_profile:
        profile = env_profile
    else:
        try:
            cfg = ModelConfig.load()
        except (OSError, ValueError):
            cfg = None
        bedrock_cfg: Mapping[str, Any] = {}
        if cfg is not None:
            bedrock_cfg = cfg.providers.get("bedrock_converse") or cfg.providers.get(
                "bedrock", {}
            )
        profile = (
            bedrock_cfg.get("aws_profile") or os.environ.get("AWS_PROFILE", "")
        ).strip()

    valid = {"auto", "sso", "static", "profile", "iam"}
    if mode not in valid:
        logger.warning("Unknown bedrock auth_mode '%s'; falling back to 'auto'", mode)
        mode = "auto"
    return mode, profile


def _build_static_creds_session(profile: str = "") -> Any:  # noqa: ANN401 — boto3.Session is dynamically typed
    """Build a boto3 Session that uses only ~/.aws/credentials (and env vars).

    Constructs a botocore Session with the SSO providers explicitly
    removed from the credential chain, so an expired SSO config in
    ~/.aws/config can't short-circuit the lookup. Used as the fallback
    leg of the ``auto`` auth mode when an SSO probe raises
    ``TokenRetrievalError``.

    Args:
        profile: Optional named profile. Empty string = default profile.

    Returns:
        A boto3.Session bound to a credentials-file-only botocore Session.
    """
    import boto3  # type: ignore[import-untyped]
    from botocore.session import (
        Session as BotocoreSession,  # type: ignore[import-untyped]
    )

    botocore_session = BotocoreSession()
    if profile:
        botocore_session.set_config_variable("profile", profile)
    # Drop SSO providers from the chain — keep env vars + ~/.aws/credentials
    # (the "shared-credentials-file" provider) + IAM. The names below are
    # the canonical botocore provider IDs.
    component = botocore_session.get_component("credential_provider")
    for sso_provider in ("sso", "sso-token"):
        try:
            component.remove(sso_provider)
        except (KeyError, ValueError):
            pass
    return boto3.Session(botocore_session=botocore_session)


def _build_bedrock_session(mode: str, profile: str) -> Any:  # noqa: ANN401 — boto3.Session is dynamically typed
    """Build the boto3 Session that the Bedrock probe + runtime should use.

    Args:
        mode: One of ``'auto'``, ``'sso'``, ``'static'``, ``'profile'``,
            ``'iam'`` — see ``_bedrock_auth_mode`` docs.
        profile: AWS profile name (used for ``'profile'`` mode and as a
            hint for ``'static'`` / ``'sso'``).

    Returns:
        A boto3.Session configured per the requested auth mode. Caller is
        responsible for catching credential-resolution exceptions.

    Raises:
        ValueError: When ``mode='profile'`` is set but no profile name
            is provided via config or env var.
    """
    import boto3  # type: ignore[import-untyped]

    if mode == "static":
        return _build_static_creds_session(profile)
    if mode == "sso":
        if profile:
            return boto3.Session(profile_name=profile)
        return boto3.Session()
    if mode == "profile":
        if not profile:
            msg = (
                "auth_mode='profile' requires a profile name — set "
                "[models.providers.bedrock] profile = ... in config.toml or "
                "BOG_AGENTS_BEDROCK_PROFILE in the environment."
            )
            raise ValueError(msg)
        return boto3.Session(profile_name=profile)
    if mode == "iam":
        # Disable env-var + shared-credentials-file + SSO providers so
        # only the IAM/instance-metadata provider runs.
        from botocore.session import (
            Session as BotocoreSession,  # type: ignore[import-untyped]
        )

        botocore_session = BotocoreSession()
        component = botocore_session.get_component("credential_provider")
        for prov in (
            "env",
            "shared-credentials-file",
            "sso",
            "sso-token",
            "assume-role",
        ):
            try:
                component.remove(prov)
            except (KeyError, ValueError):
                pass
        return boto3.Session(botocore_session=botocore_session)
    # auto — default boto3 chain
    if profile:
        return boto3.Session(profile_name=profile)
    return boto3.Session()


def _classify_bedrock_probe_failure(exc: BaseException) -> str:
    """Map a boto3 exception to a short stable classification string.

    Used as the cache key + a hint for callers that want to know *why*
    the probe failed without parsing the exception type themselves.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if name == "TokenRetrievalError" or "token has expired" in msg or "sso" in msg:
        return "sso-expired"
    if name == "NoCredentialsError" or "unable to locate credentials" in msg:
        return "no-credentials"
    return f"other:{name}"


def _check_bedrock_thorough() -> bool:
    """Resolve credentials via boto3 and verify the access key is non-empty.

    Catches expired SSO tokens and misconfigured profiles without making
    any network calls — it only checks that boto3 can produce a frozen
    credential set with a non-empty access key.

    The first failure of a given kind (sso-expired, no-credentials, etc.)
    is logged at DEBUG level *with* its traceback for diagnostic value;
    subsequent failures of the same kind log a one-line summary only.
    This keeps the log file readable when a user runs with an expired
    SSO session (issue #53 — a single ``bog-agents`` invocation
    produces 20+ identical 50-line stack traces otherwise).

    Returns:
        True if boto3 resolves valid-looking credentials.
    """
    mode, profile = _bedrock_auth_mode()

    def _probe(session_factory: Any, label: str) -> bool | None:  # noqa: ANN401
        """Run the probe with ``session_factory()``.

        Returns ``True`` on valid creds, ``False`` on no-creds (caller
        should still try fallback), ``None`` on fatal error (cached).
        """
        try:
            session = session_factory()
            creds = session.get_credentials()
            if creds is None:
                logger.debug("boto3 returned no credentials (%s)", label)
                return False
            frozen = creds.get_frozen_credentials()
            has_key = bool(frozen.access_key)
            if has_key:
                _BEDROCK_PROBE_CACHE.clear()
                logger.debug(
                    "boto3 credentials resolved (mode=%s%s, source=%s)",
                    mode,
                    f" profile={profile}" if profile else "",
                    label,
                )
                return True
            logger.debug(
                "boto3 credentials resolved but access_key is empty (%s)", label
            )
            return False
        except Exception as exc:
            kind = _classify_bedrock_probe_failure(exc)
            cached = _BEDROCK_PROBE_CACHE.get(f"{label}:{kind}")
            if cached is None:
                logger.debug(
                    "boto3 credential resolution failed (%s, %s)",
                    label,
                    kind,
                    exc_info=True,
                )
                _BEDROCK_PROBE_CACHE[f"{label}:{kind}"] = (
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                logger.debug(
                    "boto3 credential resolution failed (%s, %s; cached)", label, kind
                )
            # Auto-mode: a TokenRetrievalError on the SSO leg should not be
            # fatal — let the caller try the static-creds fallback.
            if mode == "auto" and kind == "sso-expired" and label == "default-chain":
                return False
            return None

    # Bedrock API keys (the `ABSK...` bearer token) authenticate via the
    # AWS_BEARER_TOKEN_BEDROCK env var and bypass the SigV4 credential chain
    # entirely. A user with ONLY the bearer token (no ~/.aws, no
    # AWS_ACCESS_KEY_ID) is fully able to call Bedrock, so treat the token as
    # valid credentials and skip the SigV4 probe. (REVIEW.md v2 — live-test
    # Bedrock CLI gap.) boto3>=1.39 / langchain-aws honour this token natively.
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        logger.debug("Bedrock bearer token (AWS_BEARER_TOKEN_BEDROCK) present")
        return True

    try:
        import boto3  # noqa: F401 — only here to surface ImportError cleanly
    except ImportError:
        logger.debug("boto3 not installed; cannot probe Bedrock credentials")
        return False

    if mode == "auto":
        # Try the default boto3 chain first. If SSO is expired, try the
        # static-creds-only chain so fresh ~/.aws/credentials keys work.
        result = _probe(
            lambda: _build_bedrock_session("auto", profile), "default-chain"
        )
        if result:
            return True
        if result is False:
            # The default chain didn't surface valid creds — fall back to
            # static-only. This is the issue-#54 fix: an expired SSO
            # session in ~/.aws/config short-circuits the lookup before
            # boto3 ever reads ~/.aws/credentials.
            logger.debug(
                "Default credential chain came up empty; trying static-creds fallback"
            )
            result = _probe(
                lambda: _build_static_creds_session(profile), "static-fallback"
            )
            return bool(result)
        # result is None — fatal (cached); the SSO error already surfaced
        # via the cache. Try the static fallback as a last resort so a
        # user with both expired SSO AND fresh static keys still works.
        logger.debug("SSO/default chain failed fatally; trying static-creds fallback")
        result = _probe(lambda: _build_static_creds_session(profile), "static-fallback")
        return bool(result)

    # Forced mode — single attempt, no fallback.
    result = _probe(lambda: _build_bedrock_session(mode, profile), mode)
    return bool(result)


def _check_bedrock_boto3() -> bool:
    """Delegate credential detection entirely to boto3.

    Faster than the thorough check — only asks if boto3 can locate *any*
    credential source, without freezing or inspecting the result.

    Returns:
        True if boto3 locates any credential source.
    """
    try:
        import boto3  # type: ignore[import-untyped]

        session = boto3.Session()
        creds = session.get_credentials()
        found = creds is not None
        logger.debug("boto3 credential check: found=%s", found)
        return found
    except Exception as exc:
        kind = _classify_bedrock_probe_failure(exc)
        cached = _BEDROCK_PROBE_CACHE.get(f"boto3:{kind}")
        if cached is None:
            logger.debug("boto3 credential check failed (%s)", kind, exc_info=True)
            _BEDROCK_PROBE_CACHE[f"boto3:{kind}"] = (False, str(exc))
        else:
            logger.debug("boto3 credential check failed (%s; cached)", kind)
        return False


def _check_bedrock_files() -> bool:
    """Fast file/env-var existence checks for AWS credentials.

    Does not import boto3 or validate credential freshness. Useful when
    startup speed matters or boto3 is not installed.

    Returns:
        True if any recognized AWS credential source is present.
    """
    # Explicit access key (static credentials or assumed role)
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return True

    # Named profile (SSO or otherwise)
    if os.environ.get("AWS_PROFILE"):
        return True

    # Web identity token (EKS, GitHub Actions OIDC, etc.)
    if os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
        return True

    # Session token (temporary credentials)
    if os.environ.get("AWS_SESSION_TOKEN") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True

    aws_dir = Path.home() / ".aws"

    # Shared credentials file (~/.aws/credentials)
    if (aws_dir / "credentials").is_file():
        return True

    # AWS config with SSO sessions (~/.aws/config)
    if (aws_dir / "config").is_file():
        return True

    # SSO cache directory (populated by `aws sso login`)
    sso_cache = aws_dir / "sso" / "cache"
    if sso_cache.is_dir() and any(sso_cache.iterdir()):
        return True

    return False


def get_credential_env_var(provider: str) -> str | None:
    """Return the env var name that holds credentials for a provider.

    Checks the config file first (user override), then falls back to the
    hardcoded `PROVIDER_API_KEY_ENV` map.

    Args:
        provider: Provider name.

    Returns:
        Environment variable name, or None if unknown.
    """
    config = ModelConfig.load()
    config_env = config.get_api_key_env(provider)
    if config_env:
        return config_env
    return PROVIDER_API_KEY_ENV.get(provider)


@dataclass(frozen=True)
class ModelConfig:
    """Parsed model configuration from `config.toml`.

    Instances are immutable once constructed. The `providers` mapping is
    wrapped in `MappingProxyType` to prevent accidental mutation of the
    globally cached singleton returned by `load()`.
    """

    default_model: str | None = None
    """The user's intentional default model (from config file `[models].default`)."""

    recent_model: str | None = None
    """The most recently switched-to model (from config file `[models].recent`)."""

    apply_model: str | None = None
    """Optional small fast model used for diff-applying / patch rewrites.

    Read from ``[models].apply`` in config.toml. Falls back to the main
    model if unset. The intent is the Cursor / Aider "apply model" pattern:
    a strong model reasons and emits a patch, then a small fast model is
    re-asked to literally apply that patch to the file. Splitting these
    keeps cost down on the mechanical apply step.
    """

    plan_model: str | None = None
    """Optional model to use when ``/plan`` mode is active.

    Read from ``[models].plan`` in config.toml. Lets users keep a cheap
    model for editing while routing planning turns through a stronger
    reasoning model. Falls back to the main model if unset.
    """

    fallbacks: tuple[str, ...] = ()
    """Ordered fallback model specs tried when the primary model fails.

    Each entry is a `provider:model` string (e.g., `'ollama:llama3'`).
    Populated from `[models].fallbacks` in the config file.
    """

    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    """Read-only mapping of provider names to their configurations."""

    def __post_init__(self) -> None:
        """Freeze the providers dict into a read-only proxy."""
        if not isinstance(self.providers, MappingProxyType):
            object.__setattr__(self, "providers", MappingProxyType(self.providers))

    @classmethod
    def load(cls, config_path: Path | None = None) -> ModelConfig:
        """Load config from file.

        When called with the default path, results are cached for the
        lifetime of the process. Use `clear_caches()` to reset.

        Args:
            config_path: Path to config file. Defaults to ~/.bog-agents/config.toml.

        Returns:
            Parsed `ModelConfig` instance.
                Returns empty config if file is missing, unreadable, or contains
                invalid TOML syntax.
        """
        global _default_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
        is_default = config_path is None
        if is_default and _default_config_cache is not None:
            return _default_config_cache

        if config_path is None:
            config_path = DEFAULT_CONFIG_PATH

        if not config_path.exists():
            fallback = cls()
            if is_default:
                _default_config_cache = fallback
            return fallback

        try:
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.warning(
                "Config file %s has invalid TOML syntax: %s. "
                "Ignoring config file. Fix the file or delete it to reset.",
                config_path,
                e,
            )
            fallback = cls()
            if is_default:
                _default_config_cache = fallback
            return fallback
        except (PermissionError, OSError) as e:
            logger.warning("Could not read config file %s: %s", config_path, e)
            fallback = cls()
            if is_default:
                _default_config_cache = fallback
            return fallback

        models_section = data.get("models", {})
        raw_fallbacks = models_section.get("fallbacks", [])
        if isinstance(raw_fallbacks, list):
            fallbacks = tuple(str(f) for f in raw_fallbacks)
        else:
            logger.warning("models.fallbacks should be a list; ignoring")
            fallbacks = ()
        config = cls(
            default_model=models_section.get("default"),
            recent_model=models_section.get("recent"),
            apply_model=models_section.get("apply"),
            plan_model=models_section.get("plan"),
            fallbacks=fallbacks,
            providers=models_section.get("providers", {}),
        )

        # Validate config consistency
        config._validate()

        if is_default:
            _default_config_cache = config

        return config

    def _validate(self) -> None:
        """Validate internal consistency of the config.

        Issues warnings for invalid configurations but does not raise exceptions,
        allowing the app to continue with potentially degraded functionality.
        """
        # Warn if default_model is set but doesn't use provider:model format
        if self.default_model and ":" not in self.default_model:
            logger.warning(
                "default_model '%s' should use provider:model format "
                "(e.g., 'anthropic:claude-sonnet-4-5')",
                self.default_model,
            )

        # Warn if recent_model is set but doesn't use provider:model format
        if self.recent_model and ":" not in self.recent_model:
            logger.warning(
                "recent_model '%s' should use provider:model format "
                "(e.g., 'anthropic:claude-sonnet-4-5')",
                self.recent_model,
            )

        # Validate class_path format and params references
        for name, provider in self.providers.items():
            class_path = provider.get("class_path")
            if class_path and ":" not in class_path:
                logger.warning(
                    "Provider '%s' has invalid class_path '%s': "
                    "must be in module.path:ClassName format "
                    "(e.g., 'my_package.models:MyChatModel')",
                    name,
                    class_path,
                )

            models = set(provider.get("models", []))

            params = provider.get("params", {})
            for key, value in params.items():
                if isinstance(value, dict) and key not in models:
                    logger.warning(
                        "Provider '%s' has params for '%s' "
                        "which is not in its models list",
                        name,
                        key,
                    )

    def get_all_models(self) -> list[tuple[str, str]]:
        """Get all models as `(model_name, provider_name)` tuples.

        Returns:
            List of tuples containing `(model_name, provider_name)`.
        """
        return [
            (model, provider_name)
            for provider_name, provider_config in self.providers.items()
            for model in provider_config.get("models", [])
        ]

    def get_provider_for_model(self, model_name: str) -> str | None:
        """Find the provider that contains this model.

        Args:
            model_name: The model identifier to look up.

        Returns:
            Provider name if found, None otherwise.
        """
        for provider_name, provider_config in self.providers.items():
            if model_name in provider_config.get("models", []):
                return provider_name
        return None

    def has_credentials(self, provider_name: str) -> bool | None:
        """Check if credentials are available for a provider.

        This is the config-file-driven credential check, supporting custom
        providers (e.g., local Ollama with no key required). For the hardcoded
        `PROVIDER_API_KEY_ENV`-based check used in the hot-swap path, see the
        module-level `has_provider_credentials()`.

        Args:
            provider_name: The provider to check.

        Returns:
            True if credentials are confirmed available, False if confirmed
                missing, or None if no `api_key_env` is configured and
                credential status cannot be determined.
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return False
        env_var = provider.get("api_key_env")
        if not env_var:
            return None  # No key configured — can't verify
        return bool(os.environ.get(env_var))

    def get_base_url(self, provider_name: str) -> str | None:
        """Get custom base URL.

        Args:
            provider_name: The provider to get base URL for.

        Returns:
            Base URL if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        return provider.get("base_url") if provider else None

    def get_api_key_env(self, provider_name: str) -> str | None:
        """Get the environment variable name for a provider's API key.

        Args:
            provider_name: The provider to get API key env var for.

        Returns:
            Environment variable name if configured, None otherwise.
        """
        provider = self.providers.get(provider_name)
        return provider.get("api_key_env") if provider else None

    def get_class_path(self, provider_name: str) -> str | None:
        """Get the custom class path for a provider.

        Args:
            provider_name: The provider to look up.

        Returns:
            Class path in `module.path:ClassName` format, or None.
        """
        provider = self.providers.get(provider_name)
        return provider.get("class_path") if provider else None

    def get_kwargs(
        self, provider_name: str, *, model_name: str | None = None
    ) -> dict[str, Any]:
        """Get extra constructor kwargs for a provider.

        Reads the `params` table from the provider config. Flat keys are
        provider-wide defaults; model-keyed sub-tables are per-model
        overrides that shallow-merge on top (model wins on conflict).

        Args:
            provider_name: The provider to look up.
            model_name: Optional model name for per-model overrides.

        Returns:
            Dictionary of extra kwargs (empty if none configured).
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return {}
        params = provider.get("params", {})
        result = {k: v for k, v in params.items() if not isinstance(v, dict)}
        if model_name:
            overrides = params.get(model_name)
            if isinstance(overrides, dict):
                result.update(overrides)
        return result

    def get_profile_overrides(
        self, provider_name: str, *, model_name: str | None = None
    ) -> dict[str, Any]:
        """Get profile overrides for a provider.

        Reads the `profile` table from the provider config. Flat keys are
        provider-wide defaults; model-keyed sub-tables are per-model overrides
        that shallow-merge on top (model wins on conflict).

        Args:
            provider_name: The provider to look up.
            model_name: Optional model name for per-model overrides.

        Returns:
            Dictionary of profile overrides (empty if none configured).
        """
        provider = self.providers.get(provider_name)
        if not provider:
            return {}
        profile = provider.get("profile", {})
        result = {k: v for k, v in profile.items() if not isinstance(v, dict)}
        if model_name:
            overrides = profile.get(model_name)
            if isinstance(overrides, dict):
                result.update(overrides)
        return result


def _save_model_field(
    field: str, model_spec: str, config_path: Path | None = None
) -> bool:
    """Read-modify-write a `[models].<field>` key in the config file.

    Args:
        field: Key name under the `[models]` table (e.g., `'default'` or `'recent'`).
        model_spec: The model to save in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing config or start fresh
        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        if "models" not in data:
            data["models"] = {}
        data["models"][field] = model_spec

        # Write to temp file then rename to prevent corruption if write is interrupted
        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            # Clean up temp file on any failure
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save %s model preference", field)
        return False
    else:
        # Invalidate config cache so the next load() picks up the change.
        global _default_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
        _default_config_cache = None
        return True


def save_default_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Update the default model in config file.

    Reads existing config (if any), updates `[models].default`, and writes
    back using proper TOML serialization.

    Args:
        model_spec: The model to set as default in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.

    Note:
        This function does not preserve comments in the config file.
    """
    return _save_model_field("default", model_spec, config_path)


def get_apply_model(config_path: Path | None = None) -> str | None:
    """Return the configured apply-model spec, or ``None`` if unset."""
    return ModelConfig.load(config_path).apply_model


def get_plan_model(config_path: Path | None = None) -> str | None:
    """Return the configured plan-mode model spec, or ``None`` if unset."""
    return ModelConfig.load(config_path).plan_model


def save_apply_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Persist the small / fast apply-model spec to ``[models].apply``.

    The apply-model is used by the diff-applying / patch-rewriting code
    path so heavy reasoning happens on the main model and the mechanical
    apply step uses a cheaper one. Pass an empty string to clear.
    """
    if not model_spec:
        return _clear_model_field("apply", config_path)
    return _save_model_field("apply", model_spec, config_path)


def save_plan_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Persist the plan-mode model spec to ``[models].plan``.

    When ``/plan`` is active, runs route through this model so users can
    pair a cheap apply model with a stronger planner. Pass an empty
    string to clear.
    """
    if not model_spec:
        return _clear_model_field("plan", config_path)
    return _save_model_field("plan", model_spec, config_path)


def _clear_model_field(field_name: str, config_path: Path | None = None) -> bool:
    """Delete ``[models].<field_name>`` if present."""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return True

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        models_section = data.get("models")
        if not isinstance(models_section, dict) or field_name not in models_section:
            return True
        del models_section[field_name]

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Failed to clear models.%s in %s", field_name, config_path)
        return False
    clear_caches()
    return True


def clear_default_model(config_path: Path | None = None) -> bool:
    """Remove the default model from the config file.

    Deletes the `[models].default` key so that future launches fall back to
    `[models].recent` or environment auto-detection.

    Args:
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        True if the key was removed (or was already absent), False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return True  # Nothing to clear

    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)

        models_section = data.get("models")
        if not isinstance(models_section, dict) or "default" not in models_section:
            return True  # Already absent

        del models_section["default"]

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not clear default model preference")
        return False
    else:
        global _default_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
        _default_config_cache = None
        return True


def is_warning_suppressed(key: str, config_path: Path | None = None) -> bool:
    """Check if a warning key is suppressed in the config file.

    Reads the `[warnings].suppress` list from `config.toml` and checks
    whether `key` is present.

    Args:
        key: Warning identifier to check (e.g., `'ripgrep'`).
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        `True` if the warning is suppressed, `False` otherwise (including
            when the file is missing, unreadable, or has no
            `[warnings]` section).
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        if not config_path.exists():
            return False
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug(
            "Could not read config file %s for warning suppression check",
            config_path,
            exc_info=True,
        )
        return False

    suppress_list = data.get("warnings", {}).get("suppress", [])
    if not isinstance(suppress_list, list):
        logger.debug(
            "[warnings].suppress in %s should be a list, got %s",
            config_path,
            type(suppress_list).__name__,
        )
        return False
    return key in suppress_list


def tools_auto_install(config_path: Path | None = None) -> bool:
    """Return whether managed-tool auto-install is enabled in the config file.

    Reads the `[tools].auto_install` boolean from `config.toml`. Defaults to
    `True` (auto-install on) when the file is missing, unreadable, the key is
    absent, or the value is not a boolean — so a fresh install gets a working
    `rg` without any configuration, while an explicit `auto_install = false`
    fully opts out.

    Args:
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        `True` when managed-tool auto-install is permitted, `False` only when
            `[tools].auto_install` is explicitly set to `false`.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        if not config_path.exists():
            return True
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug(
            "Could not read config file %s for auto_install check",
            config_path,
            exc_info=True,
        )
        return True

    value = data.get("tools", {}).get("auto_install", True)
    if not isinstance(value, bool):
        logger.debug(
            "[tools].auto_install in %s should be a bool, got %s",
            config_path,
            type(value).__name__,
        )
        return True
    return value


def suppress_warning(key: str, config_path: Path | None = None) -> bool:
    """Add a warning key to the suppression list in the config file.

    Reads existing config (if any), adds `key` to `[warnings].suppress`,
    and writes back using atomic temp-file rename. Deduplicates entries.

    Args:
        key: Warning identifier to suppress (e.g., `'ripgrep'`).
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        `True` if save succeeded, `False` if it failed due to I/O errors.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        if "warnings" not in data:
            data["warnings"] = {}
        suppress_list: list[str] = data["warnings"].get("suppress", [])
        if key not in suppress_list:
            suppress_list.append(key)
        data["warnings"]["suppress"] = suppress_list

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save warning suppression for '%s'", key)
        return False
    return True


THREAD_COLUMN_DEFAULTS: dict[str, bool] = {
    "thread_id": False,
    "messages": True,
    "created_at": True,
    "updated_at": True,
    "git_branch": False,
    "cwd": False,
    "initial_prompt": True,
    "agent_name": False,
}
"""Default visibility for thread selector columns."""


class ThreadConfig(NamedTuple):
    """Coalesced thread-selector configuration read from a single TOML parse."""

    columns: dict[str, bool]
    """Column visibility settings."""

    relative_time: bool
    """Whether to display timestamps as relative time."""

    sort_order: str
    """`'updated_at'` or `'created_at'`."""


_thread_config_cache: ThreadConfig | None = None


def load_thread_config(config_path: Path | None = None) -> ThreadConfig:
    """Load all thread-selector settings from one config file read.

    Returns a cached result when reading the default config path. The
    prewarm worker calls this at startup so subsequent opens of the
    `/threads` modal avoid disk I/O entirely.

    Args:
        config_path: Path to config file.

    Returns:
        Coalesced thread configuration.
    """
    global _thread_config_cache  # noqa: PLW0603  # Module-level cache requires global statement

    if config_path is None:
        if _thread_config_cache is not None:
            return _thread_config_cache
        config_path = DEFAULT_CONFIG_PATH
    use_default = config_path == DEFAULT_CONFIG_PATH

    columns = dict(THREAD_COLUMN_DEFAULTS)
    relative_time = True
    sort_order = "updated_at"

    try:
        if not config_path.exists():
            result = ThreadConfig(columns, relative_time, sort_order)
            if use_default:
                _thread_config_cache = result
            return result
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        threads_section = data.get("threads", {})

        # columns
        raw_columns = threads_section.get("columns", {})
        if isinstance(raw_columns, dict):
            for key in columns:
                if key in raw_columns and isinstance(raw_columns[key], bool):
                    columns[key] = raw_columns[key]

        # relative_time
        rt_value = threads_section.get("relative_time")
        if isinstance(rt_value, bool):
            relative_time = rt_value

        # sort_order
        so_value = threads_section.get("sort_order")
        if so_value in {"updated_at", "created_at"}:
            sort_order = so_value
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("Could not read thread config; using defaults", exc_info=True)
        # Do not cache on error — allow retry on next call in case the
        # file is fixed or permissions are restored.
        return ThreadConfig(columns, relative_time, sort_order)

    result = ThreadConfig(columns, relative_time, sort_order)
    if use_default:
        _thread_config_cache = result
    return result


def invalidate_thread_config_cache() -> None:
    """Clear the cached `ThreadConfig` so the next load re-reads disk."""
    global _thread_config_cache  # noqa: PLW0603  # Module-level cache requires global statement
    _thread_config_cache = None


def load_thread_columns(config_path: Path | None = None) -> dict[str, bool]:
    """Load thread column visibility from config file.

    Args:
        config_path: Path to config file.

    Returns:
        Dict mapping column names to visibility booleans.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    result = dict(THREAD_COLUMN_DEFAULTS)
    try:
        if not config_path.exists():
            return result
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        columns = data.get("threads", {}).get("columns", {})
        if isinstance(columns, dict):
            for key in result:
                if key in columns and isinstance(columns[key], bool):
                    result[key] = columns[key]
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read thread column config", exc_info=True)
    return result


def save_thread_columns(
    columns: dict[str, bool], config_path: Path | None = None
) -> bool:
    """Save thread column visibility to config file.

    Args:
        columns: Dict mapping column names to visibility booleans.
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        if "threads" not in data:
            data["threads"] = {}
        data["threads"]["columns"] = columns

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save thread column preferences")
        return False
    invalidate_thread_config_cache()
    return True


def load_thread_relative_time(config_path: Path | None = None) -> bool:
    """Load the relative-time display preference for thread timestamps.

    Args:
        config_path: Path to config file.

    Returns:
        True if timestamps should display as relative time.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        if not config_path.exists():
            return True
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        value = data.get("threads", {}).get("relative_time")
        if isinstance(value, bool):
            return value
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read thread relative_time config", exc_info=True)
    return True


def save_thread_relative_time(enabled: bool, config_path: Path | None = None) -> bool:
    """Save the relative-time display preference for thread timestamps.

    Args:
        enabled: Whether to display relative timestamps.
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}
        if "threads" not in data:
            data["threads"] = {}
        data["threads"]["relative_time"] = enabled
        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save thread relative_time preference")
        return False
    invalidate_thread_config_cache()
    return True


def load_thread_sort_order(config_path: Path | None = None) -> str:
    """Load the sort order preference for the thread selector.

    Args:
        config_path: Path to config file.

    Returns:
        `"updated_at"` or `"created_at"`.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        if not config_path.exists():
            return "updated_at"
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        value = data.get("threads", {}).get("sort_order")
        if value in {"updated_at", "created_at"}:
            return value
    except (OSError, tomllib.TOMLDecodeError):
        logger.debug("Could not read thread sort_order config", exc_info=True)
    return "updated_at"


def save_thread_sort_order(sort_order: str, config_path: Path | None = None) -> bool:
    """Save the sort order preference for the thread selector.

    Args:
        sort_order: `"updated_at"` or `"created_at"`.
        config_path: Path to config file.

    Returns:
        True if save succeeded, False on I/O error.

    Raises:
        ValueError: If `sort_order` is not a recognised value.
    """
    if sort_order not in {"updated_at", "created_at"}:
        msg = (
            f"Invalid sort_order {sort_order!r}; expected 'updated_at' or 'created_at'"
        )
        raise ValueError(msg)
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}
        if "threads" not in data:
            data["threads"] = {}
        data["threads"]["sort_order"] = sort_order
        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except Exception:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save thread sort_order preference")
        return False
    invalidate_thread_config_cache()
    return True


def save_recent_model(model_spec: str, config_path: Path | None = None) -> bool:
    """Update the recently used model in config file.

    Writes to `[models].recent` instead of `[models].default`, so that `/model`
    switches do not overwrite the user's intentional default.

    Args:
        model_spec: The model to save in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.

    Note:
        This function does not preserve comments in the config file.
    """
    return _save_model_field("recent", model_spec, config_path)


def save_fallbacks(fallbacks: list[str], config_path: Path | None = None) -> bool:
    """Update the fallback models list in config file.

    Writes to `[models].fallbacks` as an ordered list of `provider:model`
    strings tried when the primary model fails (auth errors, connection
    failures, etc.).

    Args:
        fallbacks: Ordered list of model specs in `provider:model` format.
        config_path: Path to config file.

            Defaults to `~/.bog-agents/config.toml`.

    Returns:
        True if save succeeded, False if it failed due to I/O errors.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        if "models" not in data:
            data["models"] = {}

        if fallbacks:
            data["models"]["fallbacks"] = fallbacks
        else:
            data["models"].pop("fallbacks", None)

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save fallback models")
        return False
    else:
        global _default_config_cache  # noqa: PLW0603
        _default_config_cache = None
        return True


def save_bedrock_credential_check(mode: str, config_path: Path | None = None) -> bool:
    """Update the Bedrock credential check mode in config file.

    Writes to `[models.providers.bedrock].credential_check`.

    Args:
        mode: One of `'thorough'`, `'boto3'`, or `'files'`.
        config_path: Path to config file. Defaults to `~/.bog-agents/config.toml`.

    Returns:
        True if save succeeded, False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        data.setdefault("models", {}).setdefault("providers", {}).setdefault(
            "bedrock", {}
        )
        data["models"]["providers"]["bedrock"]["credential_check"] = mode

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save Bedrock credential check mode")
        return False
    else:
        global _default_config_cache  # noqa: PLW0603
        _default_config_cache = None
        return True


_DEFAULT_AWS_REGION = "us-east-1"
"""Default AWS region used for Bedrock when nothing else is configured.

`us-east-1` is chosen because it has the broadest set of foundation
models available (including Anthropic Claude family, Amazon Nova,
Llama, Mistral) on Bedrock.
"""


def resolve_aws_region(
    config: ModelConfig | None = None,
    *,
    fallback: str | None = _DEFAULT_AWS_REGION,
) -> str | None:
    """Resolve the AWS region for Bedrock from config + env, in that order.

    Lookup order:

    1. `config.providers["bedrock_converse"]["region"]` (or `bedrock`)
    2. `AWS_DEFAULT_REGION` environment variable
    3. `AWS_REGION` environment variable
    4. boto3 session default (reads `~/.aws/config`)
    5. `fallback` (defaults to `us-east-1`)

    Args:
        config: Pre-loaded ModelConfig; loaded fresh when `None`.
        fallback: Region to return when nothing else resolves. Pass `None`
            to surface "no region anywhere" as `None` so the caller can
            raise a clear error.

    Returns:
        Resolved region string (e.g. `"us-east-1"`), or `fallback` if
        nothing resolves.
    """
    if config is None:
        try:
            config = ModelConfig.load()
        except (OSError, ValueError):
            config = None

    if config is not None:
        for provider_key in ("bedrock_converse", "bedrock"):
            section = config.providers.get(provider_key) or {}
            region = section.get("region") if isinstance(section, dict) else None
            if isinstance(region, str) and region.strip():
                return region.strip()

    for env_key in ("AWS_DEFAULT_REGION", "AWS_REGION"):
        env_value = os.environ.get(env_key)
        if env_value and env_value.strip():
            return env_value.strip()

    try:
        import boto3  # type: ignore[import-untyped]

        session = boto3.Session()
        if session.region_name:
            return str(session.region_name)
    except ImportError:
        pass
    except Exception:  # boto3 config probe is best-effort
        logger.debug("boto3 default region probe failed", exc_info=True)

    return fallback


def save_bedrock_region(
    region: str,
    config_path: Path | None = None,
) -> bool:
    """Persist the AWS region for Bedrock to config.toml.

    Writes `[models.providers.bedrock].region`. Subsequent CLI runs will
    use this region for Bedrock model construction without needing
    `AWS_DEFAULT_REGION` set.

    Args:
        region: AWS region (e.g. `"us-east-1"`). Whitespace is stripped.
            Empty after stripping clears the key instead of writing it.
        config_path: Path to config file. Defaults to
            `~/.bog-agents/config.toml`.

    Returns:
        True if save succeeded, False on I/O error.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    region = region.strip()

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        bedrock_section = (
            data.setdefault("models", {})
            .setdefault("providers", {})
            .setdefault("bedrock", {})
        )
        if region:
            bedrock_section["region"] = region
        else:
            bedrock_section.pop("region", None)

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save Bedrock region")
        return False
    else:
        global _default_config_cache  # noqa: PLW0603
        _default_config_cache = None
        return True


def save_bedrock_auth_mode(
    mode: str,
    profile: str | None = None,
    config_path: Path | None = None,
) -> bool:
    """Persist the Bedrock auth-mode + optional profile to config.toml.

    Writes ``[models.providers.bedrock].auth_mode`` and (optionally)
    ``[models.providers.bedrock].profile``. The next ``bog-agents``
    invocation will use the configured mode without any env vars.

    Args:
        mode: One of ``'auto'``, ``'sso'``, ``'static'``, ``'profile'``,
            ``'iam'``.
        profile: Optional AWS profile name. When None, the ``profile``
            key is left unchanged. When the empty string, the key is
            removed.
        config_path: Path to config file. Defaults to
            ``~/.bog-agents/config.toml``.

    Returns:
        True if save succeeded, False on I/O error.

    Raises:
        ValueError: If *mode* is not one of the supported strings.
    """
    valid_modes = {"auto", "sso", "static", "profile", "iam"}
    if mode not in valid_modes:
        msg = f"invalid auth_mode '{mode}'; must be one of {sorted(valid_modes)}"
        raise ValueError(msg)

    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)
        else:
            data = {}

        bedrock_section = (
            data.setdefault("models", {})
            .setdefault("providers", {})
            .setdefault("bedrock", {})
        )
        bedrock_section["auth_mode"] = mode
        if profile is not None:
            if profile:
                bedrock_section["aws_profile"] = profile
            else:
                bedrock_section.pop("aws_profile", None)

        fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            Path(tmp_path).replace(config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Could not save Bedrock auth mode")
        return False
    else:
        global _default_config_cache  # noqa: PLW0603
        _default_config_cache = None
        return True
