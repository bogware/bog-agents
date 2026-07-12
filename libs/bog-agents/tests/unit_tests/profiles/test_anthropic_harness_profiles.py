"""Unit tests for the built-in Anthropic harness profiles.

Each `_anthropic_*` module exposes a module-level `register()` that layers
a system-prompt suffix onto its `anthropic:<model>` key. These tests
register into an isolated snapshot of the global registry (restored after
each test) and assert both the model key each module targets and that the
suffix text carries the expected upstream markers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bog_agents.profiles.harness import _anthropic_haiku_4_5, _anthropic_opus_4_7, _anthropic_sonnet_4_6, harness_profiles as hp
from bog_agents.profiles.harness.harness_profiles import _get_harness_profile

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Snapshot and restore the global harness registry around each test."""
    saved = dict(hp._HARNESS_PROFILES)
    hp._HARNESS_PROFILES.clear()
    try:
        yield
    finally:
        hp._HARNESS_PROFILES.clear()
        hp._HARNESS_PROFILES.update(saved)


# Universal Claude markers present in every Anthropic harness suffix.
_UNIVERSAL_MARKERS = (
    "<use_parallel_tool_calls>",
    "<investigate_before_answering>",
    "<tool_result_reflection>",
)


def test_opus_registers_model_key_with_markers() -> None:
    _anthropic_opus_4_7.register()
    profile = _get_harness_profile("anthropic:claude-opus-4-7")
    assert profile is not None
    suffix = profile.system_prompt_suffix
    assert suffix is not None
    for marker in _UNIVERSAL_MARKERS:
        assert marker in suffix
    # Opus-specific overlays that counter reduced tool/subagent eagerness.
    assert "<tool_usage>" in suffix
    assert "<subagent_usage>" in suffix


def test_sonnet_registers_model_key_with_universal_markers_only() -> None:
    _anthropic_sonnet_4_6.register()
    profile = _get_harness_profile("anthropic:claude-sonnet-4-6")
    assert profile is not None
    suffix = profile.system_prompt_suffix
    assert suffix is not None
    for marker in _UNIVERSAL_MARKERS:
        assert marker in suffix
    # Sonnet 4.6 carries no model-specific overlays.
    assert "<tool_usage>" not in suffix
    assert "<subagent_usage>" not in suffix


def test_haiku_registers_model_key_with_universal_markers_only() -> None:
    _anthropic_haiku_4_5.register()
    profile = _get_harness_profile("anthropic:claude-haiku-4-5")
    assert profile is not None
    suffix = profile.system_prompt_suffix
    assert suffix is not None
    for marker in _UNIVERSAL_MARKERS:
        assert marker in suffix
    # Haiku 4.5 carries no model-specific overlays.
    assert "<tool_usage>" not in suffix
    assert "<subagent_usage>" not in suffix


def test_each_module_targets_exactly_its_own_key() -> None:
    _anthropic_opus_4_7.register()
    _anthropic_sonnet_4_6.register()
    _anthropic_haiku_4_5.register()
    assert set(hp._HARNESS_PROFILES) == {
        "anthropic:claude-opus-4-7",
        "anthropic:claude-sonnet-4-6",
        "anthropic:claude-haiku-4-5",
    }
