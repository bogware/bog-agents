"""Tests for native, per-provider reasoning-effort mapping."""

from __future__ import annotations

import pytest

from bog_agents_cli.reasoning_effort import (
    ANTHROPIC_EFFORTS,
    ANTHROPIC_EFFORTS_NO_MAX,
    ANTHROPIC_EFFORTS_NO_XHIGH,
    LEGACY_EFFORT_LEVELS,
    OPENAI_56_EFFORTS,
    OPENAI_EFFORTS,
    default_effort_for_model,
    effort_levels_for_model,
    model_params_for_effort,
    render_effort_status,
    supported_efforts_for_model,
)


class TestSupportedEffortsAnthropic:
    """Per-version Anthropic capability detection."""

    def test_opus_4_8_full_range(self) -> None:
        assert supported_efforts_for_model("anthropic:claude-opus-4-8") == (
            ANTHROPIC_EFFORTS
        )

    def test_opus_4_6_has_no_xhigh(self) -> None:
        got = supported_efforts_for_model("anthropic:claude-opus-4-6")
        assert got == ANTHROPIC_EFFORTS_NO_XHIGH
        assert "xhigh" not in got

    def test_opus_4_5_has_no_max_or_xhigh(self) -> None:
        got = supported_efforts_for_model("anthropic:claude-opus-4-5")
        assert got == ANTHROPIC_EFFORTS_NO_MAX
        assert "max" not in got
        assert "xhigh" not in got

    def test_opus_4_1_predates_effort(self) -> None:
        assert supported_efforts_for_model("anthropic:claude-opus-4-1") == ()

    def test_sonnet_5_full_range(self) -> None:
        assert supported_efforts_for_model("anthropic:claude-sonnet-5") == (
            ANTHROPIC_EFFORTS
        )

    def test_sonnet_4_6_no_xhigh(self) -> None:
        assert supported_efforts_for_model("anthropic:claude-sonnet-4-6") == (
            ANTHROPIC_EFFORTS_NO_XHIGH
        )

    def test_sonnet_4_5_rejects_effort(self) -> None:
        assert supported_efforts_for_model("anthropic:claude-sonnet-4-5") == ()

    def test_dated_suffix_still_matches(self) -> None:
        # A dated model id must classify by its version, not read as a newer one.
        assert (
            supported_efforts_for_model("anthropic:claude-opus-4-6-20991231")
            == ANTHROPIC_EFFORTS_NO_XHIGH
        )


class TestSupportedEffortsOpenAI:
    def test_gpt_5_4_base_range(self) -> None:
        got = supported_efforts_for_model("openai:gpt-5.4")
        assert got == OPENAI_EFFORTS
        assert "max" not in got

    def test_gpt_5_6_adds_max(self) -> None:
        got = supported_efforts_for_model("openai:gpt-5.6")
        assert got == OPENAI_56_EFFORTS
        assert "max" in got

    def test_non_gpt5_openai_unsupported(self) -> None:
        assert supported_efforts_for_model("openai:gpt-4o") == ()


class TestSupportedEffortsOtherProviders:
    def test_gemini_3(self) -> None:
        assert supported_efforts_for_model("google_genai:gemini-3-pro") == (
            "low",
            "medium",
            "high",
        )

    def test_gemini_non_3_unsupported(self) -> None:
        assert supported_efforts_for_model("google_genai:gemini-2.5-flash") == ()

    def test_fireworks_deepseek(self) -> None:
        got = supported_efforts_for_model(
            "fireworks:accounts/fireworks/models/deepseek-v4-pro"
        )
        assert got == ("none", "low", "medium", "high", "xhigh", "max")

    def test_fireworks_kimi(self) -> None:
        got = supported_efforts_for_model(
            "fireworks:accounts/fireworks/models/kimi-k2-instruct"
        )
        assert got == ("low", "medium", "high")

    def test_fireworks_glm(self) -> None:
        got = supported_efforts_for_model("fireworks:accounts/fireworks/models/glm-5p2")
        assert got == ("none", "high", "max")

    def test_xai_grok_45(self) -> None:
        assert supported_efforts_for_model("xai:grok-4.5") == ("low", "medium", "high")

    def test_xai_other_unsupported(self) -> None:
        assert supported_efforts_for_model("xai:grok-3") == ()


class TestSupportedEffortsEdgeCases:
    def test_none_spec(self) -> None:
        assert supported_efforts_for_model(None) == ()

    def test_malformed_spec(self) -> None:
        assert supported_efforts_for_model("no-colon-here") == ()

    def test_unknown_provider(self) -> None:
        assert supported_efforts_for_model("ollama:llama3") == ()


class TestModelParamsTranslation:
    """Each provider maps `/effort` onto its real reasoning knob."""

    def test_anthropic_uses_output_config_effort(self) -> None:
        params = model_params_for_effort("anthropic:claude-opus-4-8", "high")
        assert params is not None
        assert params["output_config"] == {"effort": "high"}
        assert params["thinking"] == {"type": "adaptive", "display": "summarized"}

    def test_openai_uses_reasoning_effort(self) -> None:
        params = model_params_for_effort("openai:gpt-5.6", "high")
        assert params == {"reasoning": {"effort": "high", "summary": "auto"}}

    def test_openai_none_omits_summary(self) -> None:
        params = model_params_for_effort("openai:gpt-5.4", "none")
        assert params == {"reasoning": {"effort": "none"}}

    def test_gemini_uses_thinking_level(self) -> None:
        params = model_params_for_effort("google_genai:gemini-3-pro", "medium")
        assert params == {"thinking_level": "medium"}

    def test_fireworks_uses_model_kwargs(self) -> None:
        params = model_params_for_effort(
            "fireworks:accounts/fireworks/models/deepseek-v4-pro", "max"
        )
        assert params == {"model_kwargs": {"reasoning_effort": "max"}}

    def test_xai_uses_extra_body(self) -> None:
        params = model_params_for_effort("xai:grok-4.5", "low")
        assert params == {"extra_body": {"reasoning_effort": "low"}}

    def test_reasoning_model_never_gets_max_tokens(self) -> None:
        # The central regression: a reasoning model must NEVER receive the
        # truncating max_tokens/temperature preset for any level.
        for level in ("none", "low", "medium", "high", "xhigh", "max"):
            for spec in (
                "anthropic:claude-opus-4-8",
                "openai:gpt-5.6",
                "google_genai:gemini-3-pro",
                "fireworks:accounts/fireworks/models/deepseek-v4-pro",
                "xai:grok-4.5",
            ):
                params = model_params_for_effort(spec, level)
                if params is None:
                    continue
                assert "max_tokens" not in params, (spec, level)
                assert "temperature" not in params, (spec, level)

    def test_unsupported_level_returns_none(self) -> None:
        # `max` is not accepted by Opus 4.5.
        assert model_params_for_effort("anthropic:claude-opus-4-5", "max") is None

    def test_non_reasoning_model_returns_none(self) -> None:
        assert model_params_for_effort("openai:gpt-4o", "high") is None

    def test_none_spec_returns_none(self) -> None:
        assert model_params_for_effort(None, "high") is None


class TestDefaultEffort:
    def test_anthropic_default_high(self) -> None:
        assert default_effort_for_model("anthropic:claude-opus-4-8") == "high"

    def test_openai_5_6_default_medium(self) -> None:
        assert default_effort_for_model("openai:gpt-5.6") == "medium"

    def test_unknown_default_none(self) -> None:
        assert default_effort_for_model("openai:gpt-5.4") is None

    def test_non_reasoning_default_none(self) -> None:
        assert default_effort_for_model("openai:gpt-4o") is None


class TestEffortLevelsForModel:
    """The vocabulary `/effort` validates a request against."""

    def test_reasoning_model_uses_native_set(self) -> None:
        assert effort_levels_for_model("anthropic:claude-opus-4-8") == ANTHROPIC_EFFORTS

    def test_non_reasoning_model_uses_legacy(self) -> None:
        assert effort_levels_for_model("openai:gpt-4o") == LEGACY_EFFORT_LEVELS

    def test_none_spec_uses_legacy(self) -> None:
        assert effort_levels_for_model(None) == LEGACY_EFFORT_LEVELS

    def test_result_never_empty(self) -> None:
        for spec in (None, "openai:gpt-4o", "anthropic:claude-sonnet-4-5"):
            assert effort_levels_for_model(spec)


class TestRenderEffortStatus:
    def test_reasoning_model_lists_native_and_default(self) -> None:
        text = render_effort_status("anthropic:claude-opus-4-8", "high")
        assert "Current effort: high" in text
        assert "Native reasoning levels" in text
        assert "model default: high" in text
        assert "xhigh" in text
        assert "Usage: /effort low|medium|high|xhigh|max" in text

    def test_non_reasoning_model_notes_preset_fallback(self) -> None:
        text = render_effort_status("openai:gpt-4o", "medium")
        assert "Current effort: medium" in text
        assert "no native reasoning control" in text
        assert "Usage: /effort low|medium|high|max" in text


def test_provider_config_vocab_matches_at_import() -> None:
    """The import-time provider-vocab assert must be present and satisfied.

    Importing the module (done at the top of this file) runs the assert; if
    the provider configs and the `ReasoningProvider` vocabulary diverged the
    import would already have raised. This test documents the invariant.
    """
    from typing import get_args

    from bog_agents_cli.reasoning_effort import (
        _PROVIDER_CONFIGS,
        ReasoningProvider,
    )

    assert set(_PROVIDER_CONFIGS) == set(get_args(ReasoningProvider))


@pytest.mark.parametrize(
    ("spec", "expected_default"),
    [
        ("google_genai:gemini-3.5-flash", "medium"),
        ("google_genai:gemini-3-pro", "high"),
        ("fireworks:accounts/fireworks/models/deepseek-v4-pro", "high"),
        ("xai:grok-4.5", "high"),
    ],
)
def test_provider_defaults(spec: str, expected_default: str) -> None:
    assert default_effort_for_model(spec) == expected_default
