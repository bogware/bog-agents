"""Unit tests for the built-in provider profiles (openai, nvidia, openrouter).

These exercise each profile module's `register()` and helper functions in
isolation from the lazy bootstrap: the tests register directly into the live
registry and restore it afterward, so they do not depend on
`_ensure_builtin_profiles_loaded` wiring the built-ins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.profiles.provider import _nvidia, _openai, _openrouter, provider_profiles as pp
from bog_agents.profiles.provider.provider_profiles import apply_provider_profile, get_provider_profile

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Snapshot and restore the provider registry around each test."""
    saved = dict(pp._PROVIDER_PROFILES)
    pp._PROVIDER_PROFILES.clear()
    try:
        yield
    finally:
        pp._PROVIDER_PROFILES.clear()
        pp._PROVIDER_PROFILES.update(saved)


@pytest.fixture
def _clear_openrouter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no OpenRouter-related env vars leak in from the host."""
    for name in (
        "OPENROUTER_APP_URL",
        "OPENROUTER_APP_TITLE",
        _openrouter._OPENROUTER_ALLOW_AZURE_ENV,
        _openrouter._OPENROUTER_ALLOW_AZURE_ENV_LEGACY,
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# OpenAI profile
# ---------------------------------------------------------------------------


def test_openai_register_sets_use_responses_api() -> None:
    _openai.register()
    profile = get_provider_profile("openai")
    assert profile is not None
    assert profile.init_kwargs["use_responses_api"] is True


def test_openai_apply_forwards_use_responses_api() -> None:
    _openai.register()
    merged = apply_provider_profile("openai:gpt-5.4")
    assert merged["use_responses_api"] is True


def test_openai_caller_can_override_responses_api() -> None:
    _openai.register()
    merged = apply_provider_profile("openai", {"use_responses_api": False})
    assert merged["use_responses_api"] is False


# ---------------------------------------------------------------------------
# NVIDIA profile
# ---------------------------------------------------------------------------


def test_nvidia_register_injects_billing_origin_header() -> None:
    _nvidia.register()
    merged = apply_provider_profile("nvidia")
    headers = merged["default_headers"]
    assert headers[_nvidia._NVIDIA_BILLING_ORIGIN_HEADER] == "BogAgents"


def test_nvidia_header_name_is_billing_invoke_origin() -> None:
    assert _nvidia._NVIDIA_BILLING_ORIGIN_HEADER == "X-BILLING-INVOKE-ORIGIN"


def test_nvidia_returns_fresh_mapping_each_call() -> None:
    first = _nvidia._nvidia_attribution_kwargs()
    second = _nvidia._nvidia_attribution_kwargs()
    assert first == second
    assert first["default_headers"] is not second["default_headers"]


# ---------------------------------------------------------------------------
# OpenRouter profile — Azure routing prevention (the high-value fix)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_ignores_azure_by_default() -> None:
    _openrouter.register()
    merged = apply_provider_profile("openrouter:openai/gpt-5")
    assert merged["openrouter_provider"] == {"ignore": ["azure"]}


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_default_attribution_headers() -> None:
    _openrouter.register()
    merged = apply_provider_profile("openrouter")
    assert merged["app_url"] == _openrouter._OPENROUTER_APP_URL
    assert merged["app_title"] == _openrouter._OPENROUTER_APP_TITLE


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_env_vars_suppress_default_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_APP_URL", "https://example.com")
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "Custom")
    _openrouter.register()
    merged = apply_provider_profile("openrouter")
    # SDK defaults deferred to the user's env vars: keys absent so ChatOpenRouter
    # reads them from env itself.
    assert "app_url" not in merged
    assert "app_title" not in merged


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_empty_env_string_suppresses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_APP_URL", "")
    _openrouter.register()
    merged = apply_provider_profile("openrouter")
    assert "app_url" not in merged
    assert merged["app_title"] == _openrouter._OPENROUTER_APP_TITLE


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_allow_azure_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_openrouter._OPENROUTER_ALLOW_AZURE_ENV, "1")
    _openrouter.register()
    merged = apply_provider_profile("openrouter")
    assert "openrouter_provider" not in merged


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_legacy_allow_azure_env_still_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_openrouter._OPENROUTER_ALLOW_AZURE_ENV_LEGACY, "true")
    _openrouter.register()
    merged = apply_provider_profile("openrouter")
    assert "openrouter_provider" not in merged


@pytest.mark.usefixtures("_clear_openrouter_env")
@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "bogus"])
def test_openrouter_non_truthy_allow_azure_keeps_ignore(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_openrouter._OPENROUTER_ALLOW_AZURE_ENV, value)
    _openrouter.register()
    merged = apply_provider_profile("openrouter")
    assert merged["openrouter_provider"] == {"ignore": ["azure"]}


@pytest.mark.usefixtures("_clear_openrouter_env")
@pytest.mark.parametrize("value", ["1", "TRUE", "Yes", "on", " on "])
def test_openrouter_truthy_variants_allow_azure(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_openrouter._OPENROUTER_ALLOW_AZURE_ENV, value)
    assert _openrouter._allow_azure() is True


# ---------------------------------------------------------------------------
# OpenRouter version guard (pre_init)
# ---------------------------------------------------------------------------


def test_openrouter_version_check_skipped_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> str:
        raise _openrouter.PackageNotFoundError

    monkeypatch.setattr(_openrouter, "pkg_version", _raise)
    _openrouter.check_openrouter_version()  # no raise


def test_openrouter_version_check_raises_when_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_openrouter, "pkg_version", lambda _name: "0.1.0")
    with pytest.raises(ImportError, match="langchain-openrouter"):
        _openrouter.check_openrouter_version()


def test_openrouter_version_check_passes_when_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_openrouter, "pkg_version", lambda _name: "0.2.0")
    _openrouter.check_openrouter_version()  # no raise


def test_openrouter_version_check_skips_non_pep440(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_openrouter, "pkg_version", lambda _name: "not-a-version")
    _openrouter.check_openrouter_version()  # no raise — logged and skipped


@pytest.mark.usefixtures("_clear_openrouter_env")
def test_openrouter_pre_init_runs_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_openrouter, "pkg_version", lambda _name: "0.1.0")
    _openrouter.register()
    with pytest.raises(ImportError, match="langchain-openrouter"):
        apply_provider_profile("openrouter")
