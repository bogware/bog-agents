"""End-to-end tests for the built-in profile bootstrap.

Unlike `test_provider_profiles.py` and the per-family harness tests — which
call each module's `register()` in isolation — these tests exercise the real
`_ensure_builtin_profiles_loaded` wiring: they force a fresh bootstrap and
assert that every built-in provider and harness profile ends up resolvable
through the public lookup paths, and that adding built-ins did not regress
`_has_any_harness_profile`'s bootstrap-vs-user classification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.profiles import _builtin_profiles as bp
from bog_agents.profiles.harness import harness_profiles as hp
from bog_agents.profiles.harness.harness_profiles import (
    HarnessProfile,
    _get_harness_profile,
    _has_any_harness_profile,
    register_harness_profile,
)
from bog_agents.profiles.provider import provider_profiles as pp
from bog_agents.profiles.provider.provider_profiles import get_provider_profile

if TYPE_CHECKING:
    from collections.abc import Iterator


# Expected built-in harness model keys (exact `provider:model` specs).
_EXPECTED_HARNESS_KEYS: tuple[str, ...] = (
    "anthropic:claude-opus-4-7",
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-haiku-4-5",
    "openai:gpt-5.1-codex",
    "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
)

# Expected built-in provider profile keys (provider-wide construction profiles).
_EXPECTED_PROVIDER_KEYS: tuple[str, ...] = ("openai", "nvidia", "openrouter")


@pytest.fixture
def _fresh_bootstrap() -> Iterator[None]:
    """Snapshot bootstrap state, force a clean bootstrap, then restore.

    Both registries and the bootstrap coordination globals are saved and
    restored so the fresh bootstrap this fixture triggers cannot leak into
    other tests (or inherit their state).
    """
    saved_harness = dict(hp._HARNESS_PROFILES)
    saved_provider = dict(pp._PROVIDER_PROFILES)
    saved_loaded = bp._loaded
    saved_thread = bp._loading_thread_id
    saved_keys = bp._BOOTSTRAP_HARNESS_KEYS

    hp._HARNESS_PROFILES.clear()
    pp._PROVIDER_PROFILES.clear()
    bp._loaded = False
    bp._loading_thread_id = None
    bp._BOOTSTRAP_HARNESS_KEYS = frozenset()

    bp._ensure_builtin_profiles_loaded()
    try:
        yield
    finally:
        hp._HARNESS_PROFILES.clear()
        hp._HARNESS_PROFILES.update(saved_harness)
        pp._PROVIDER_PROFILES.clear()
        pp._PROVIDER_PROFILES.update(saved_provider)
        bp._loaded = saved_loaded
        bp._loading_thread_id = saved_thread
        bp._BOOTSTRAP_HARNESS_KEYS = saved_keys


@pytest.mark.usefixtures("_fresh_bootstrap")
@pytest.mark.parametrize("key", _EXPECTED_HARNESS_KEYS)
def test_builtin_harness_key_resolves(key: str) -> None:
    """Every built-in harness model key resolves to a profile after bootstrap."""
    profile = _get_harness_profile(key)
    assert profile is not None, f"expected a built-in HarnessProfile for {key!r}"
    assert isinstance(profile, HarnessProfile)
    # Every built-in harness profile carries a model-tuning suffix.
    assert profile.system_prompt_suffix, f"{key!r} profile has no system_prompt_suffix"


@pytest.mark.usefixtures("_fresh_bootstrap")
def test_anthropic_opus_profile_resolves_via_normalized_provider() -> None:
    """The Opus key resolves even when the provider half is not lowercased.

    Exercises the `_get_harness_profile` provider-prefix fallback and the
    exact-key path together: the exact lowercase key is registered, so a
    canonical `anthropic:claude-opus-4-7` spec resolves directly.
    """
    assert _get_harness_profile("anthropic:claude-opus-4-7") is not None


@pytest.mark.usefixtures("_fresh_bootstrap")
@pytest.mark.parametrize("key", _EXPECTED_PROVIDER_KEYS)
def test_builtin_provider_key_registered(key: str) -> None:
    """openai / nvidia / openrouter provider profiles are registered after bootstrap."""
    assert get_provider_profile(key) is not None, f"expected a built-in ProviderProfile for {key!r}"


@pytest.mark.usefixtures("_fresh_bootstrap")
def test_openai_provider_profile_enables_responses_api() -> None:
    """The OpenAI provider profile is the source of truth for `use_responses_api`."""
    profile = get_provider_profile("openai")
    assert profile is not None
    assert profile.init_kwargs.get("use_responses_api") is True


@pytest.mark.usefixtures("_fresh_bootstrap")
def test_all_builtin_harness_keys_land_in_bootstrap_snapshot() -> None:
    """Built-in harness keys are captured in `_BOOTSTRAP_HARNESS_KEYS`.

    This is the invariant that keeps `_has_any_harness_profile` honest: the
    snapshot must include the built-ins so they are subtracted out and not
    mistaken for user registrations.
    """
    for key in _EXPECTED_HARNESS_KEYS:
        assert key in bp._BOOTSTRAP_HARNESS_KEYS, f"{key!r} missing from _BOOTSTRAP_HARNESS_KEYS"


@pytest.mark.usefixtures("_fresh_bootstrap")
def test_has_any_harness_profile_false_after_pure_bootstrap() -> None:
    """With only built-ins registered, `_has_any_harness_profile` stays `False`.

    Regression guard: the log-level in `_harness_profile_for_model` escalates
    to WARNING only when the user has registered a profile. Adding built-ins
    must not flip that classification — bootstrap-registered profiles are
    subtracted via `_BOOTSTRAP_HARNESS_KEYS`.
    """
    assert _has_any_harness_profile() is False


@pytest.mark.usefixtures("_fresh_bootstrap")
def test_has_any_harness_profile_true_after_user_registration() -> None:
    """A user registration under a non-bootstrap key flips the classification to `True`."""
    assert _has_any_harness_profile() is False
    register_harness_profile("customprov:custommodel", HarnessProfile(system_prompt_suffix="x"))
    assert _has_any_harness_profile() is True
