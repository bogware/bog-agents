"""Built-in OpenAI provider profile.

Enables the OpenAI Responses API by default for all `openai:*` models via
`use_responses_api=True`. Users may layer additional kwargs on top via
`register_provider_profile("openai", ...)`.

Registered by `_ensure_builtin_profiles_loaded` during the first
profile-registry access. Not exposed as an `importlib.metadata` entry point —
built-ins ship with the SDK and should not depend on install-time metadata to
activate.

This profile is the source of truth for the Responses-API default: the
`_apply_openai_responses_default` bridge in `bog_agents._models` only sets
`use_responses_api=True` when the key is absent, so once this profile
registers the key the bridge no-ops.
"""

from __future__ import annotations

from bog_agents.profiles.provider.provider_profiles import (
    ProviderProfile,
    _register_provider_profile_impl,
)


def register() -> None:
    """Register the built-in OpenAI provider profile."""
    _register_provider_profile_impl(
        "openai",
        ProviderProfile(init_kwargs={"use_responses_api": True}),
    )
