"""Unit tests for the built-in OpenAI Codex harness profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.profiles.harness import _openai_codex, harness_profiles as hp
from bog_agents.profiles.harness.harness_profiles import (
    HarnessProfile,
    _get_harness_profile,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Snapshot and restore the harness registry around each test."""
    saved = dict(hp._HARNESS_PROFILES)
    hp._HARNESS_PROFILES.clear()
    try:
        yield
    finally:
        hp._HARNESS_PROFILES.clear()
        hp._HARNESS_PROFILES.update(saved)


def test_register_populates_all_codex_specs() -> None:
    _openai_codex.register()
    for spec in _openai_codex._CODEX_MODEL_SPECS:
        assert spec in hp._HARNESS_PROFILES


def test_registered_specs_are_the_gpt_5x_codex_family() -> None:
    assert _openai_codex._CODEX_MODEL_SPECS == (
        "openai:gpt-5.1-codex",
        "openai:gpt-5.2-codex",
        "openai:gpt-5.3-codex",
    )


@pytest.mark.parametrize("spec", _openai_codex._CODEX_MODEL_SPECS)
def test_each_spec_resolves_to_suffix_profile(spec: str) -> None:
    _openai_codex.register()
    profile = _get_harness_profile(spec)
    assert profile is not None
    assert profile.system_prompt_suffix == _openai_codex._SYSTEM_PROMPT_SUFFIX


def test_suffix_carries_codex_behavior_directives() -> None:
    suffix = _openai_codex._SYSTEM_PROMPT_SUFFIX
    # The behavior fix: Codex stops early without these directives.
    assert "autonomous senior engineer" in suffix
    assert "Bias to action" in suffix
    assert "Just act." in suffix
    assert "## Parallel Tool Use" in suffix
    assert "## Plan Hygiene" in suffix
    assert "write_todos" in suffix


def test_profile_only_sets_suffix() -> None:
    """The Codex profile shapes prose only — it does not touch tools/middleware."""
    _openai_codex.register()
    profile = _get_harness_profile("openai:gpt-5.1-codex")
    assert profile is not None
    assert profile.base_system_prompt is None
    assert profile.excluded_tools == frozenset()
    assert profile.excluded_middleware == frozenset()
    assert profile.tool_description_overrides == {}
    assert profile.general_purpose_subagent is None


def test_non_codex_openai_model_is_unaffected() -> None:
    """Per-model keys leave stock OpenAI models with no harness profile."""
    _openai_codex.register()
    assert _get_harness_profile("openai:gpt-5.4") is None
    # No provider-wide `openai` key is registered by this module.
    assert "openai" not in hp._HARNESS_PROFILES


def test_register_is_idempotent_via_additive_merge() -> None:
    """Re-registering merges on top and keeps the same suffix."""
    _openai_codex.register()
    _openai_codex.register()
    profile = _get_harness_profile("openai:gpt-5.2-codex")
    assert profile is not None
    assert profile.system_prompt_suffix == _openai_codex._SYSTEM_PROMPT_SUFFIX


def test_apply_profile_prompt_appends_suffix() -> None:
    """The suffix layers onto a base prompt with a blank-line separator."""
    profile = HarnessProfile(system_prompt_suffix=_openai_codex._SYSTEM_PROMPT_SUFFIX)
    result = hp._apply_profile_prompt(profile, "BASE")
    assert result == "BASE\n\n" + _openai_codex._SYSTEM_PROMPT_SUFFIX
