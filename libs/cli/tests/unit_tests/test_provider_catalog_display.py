"""Tests for display-name derivation, thinking detection, and catalog cache.

These exercises pin the user-visible behaviour of the `provider_catalog`
helpers added for the model-picker refinements:

- `derive_model_display` produces stable human-readable labels.
- `supports_native_thinking` returns the expected verdict per family.
- `load_cached_catalog` / `save_cached_catalog` round-trip a real
  catalog through a temp directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bog_agents_cli.provider_catalog import (
    DEFAULT_MODEL_CANDIDATES,
    ModelDisplay,
    clear_cached_catalog,
    derive_model_display,
    get_provider_display_name,
    load_cached_catalog,
    save_cached_catalog,
    supports_native_thinking,
)


class TestProviderDisplayName:
    """``get_provider_display_name`` maps provider ids to human labels."""

    def test_known_provider(self) -> None:
        assert get_provider_display_name("anthropic") == "Anthropic"
        assert get_provider_display_name("bedrock_converse") == "AWS Bedrock"
        assert get_provider_display_name("google_genai") == "Google Gemini"

    def test_unknown_provider_falls_back_to_title_case(self) -> None:
        assert get_provider_display_name("my_custom_provider") == "My Custom Provider"


class TestDeriveModelDisplay:
    """Display labels for common provider:model combinations."""

    def test_claude_anthropic(self) -> None:
        d = derive_model_display("anthropic", "claude-sonnet-4-6")
        assert d.display_name == "Claude Sonnet 4.6"
        assert d.family == "claude"
        assert d.vendor == "anthropic"
        assert d.supports_thinking is True
        assert d.is_inference_profile is False

    def test_claude_haiku(self) -> None:
        d = derive_model_display("anthropic", "claude-haiku-4-5")
        assert d.display_name == "Claude Haiku 4.5"

    def test_bedrock_inference_profile_tagged(self) -> None:
        d = derive_model_display("bedrock_converse", "us.anthropic.claude-sonnet-4-6")
        assert d.is_inference_profile is True
        # Display name should contain 'Bedrock US' to make the region obvious.
        assert "Bedrock US" in d.display_name
        assert "Claude Sonnet 4.6" in d.display_name
        assert d.supports_thinking is True
        assert d.vendor == "anthropic"

    def test_bedrock_base_id_not_profile(self) -> None:
        d = derive_model_display("bedrock_converse", "anthropic.claude-sonnet-4-6")
        assert d.is_inference_profile is False
        assert "Bedrock" not in d.display_name  # base ids don't get the region suffix
        assert d.vendor == "anthropic"
        assert d.supports_thinking is True

    def test_gemini(self) -> None:
        d = derive_model_display("google_genai", "gemini-2.5-pro")
        assert d.display_name == "Gemini 2.5 Pro"
        assert d.supports_thinking is True

    def test_gpt(self) -> None:
        d = derive_model_display("openai", "gpt-5.4-mini")
        assert d.display_name == "GPT-5.4 Mini"
        # GPT family is not native-thinking; o-series is.
        assert d.supports_thinking is False

    def test_o_series_supports_thinking(self) -> None:
        d = derive_model_display("openai", "o1-mini")
        assert d.family == "o-series"
        assert d.supports_thinking is True

    def test_nova(self) -> None:
        d = derive_model_display("bedrock_converse", "us.amazon.nova-pro-v1:0")
        assert d.family == "nova"
        assert "Nova" in d.display_name

    def test_ollama_unknown_falls_back_gracefully(self) -> None:
        # The qwen prefix should yield a sensible fallback rather than crashing.
        d = derive_model_display("ollama", "qwen3:4b")
        assert d.spec == "ollama:qwen3:4b"
        assert d.display_name  # non-empty
        assert d.supports_thinking is False


class TestSupportsNativeThinking:
    """Catalog mirrors the SDK's `_model_supports_native_thinking` rules."""

    @pytest.mark.parametrize(
        "model",
        [
            "claude-3-7-sonnet-20250219",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-haiku-4-5",
            "gemini-2.5-pro",
            "gemini-3-flash-preview",
            "o1",
            "o3-mini",
            "o4",
        ],
    )
    def test_thinking_supported(self, model: str) -> None:
        assert supports_native_thinking(model) is True

    @pytest.mark.parametrize(
        "model",
        [
            "claude-3-5-sonnet-20240620",
            "gpt-4o",
            "gpt-5.4",
            "gemini-1.5-pro",
            "llama-3.3-70b",
            "qwen3:4b",
        ],
    )
    def test_thinking_not_supported(self, model: str) -> None:
        assert supports_native_thinking(model) is False


class TestCachedCatalog:
    """`save_cached_catalog` + `load_cached_catalog` round-trip cleanly."""

    def test_round_trip(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "models.cache.json"
        catalog = {
            "anthropic": ["claude-sonnet-4-6", "claude-haiku-4-5"],
            "openai": ["gpt-5.4"],
        }
        assert save_cached_catalog(catalog, path=cache_file) is True
        loaded = load_cached_catalog(path=cache_file)
        assert loaded is not None
        assert loaded["anthropic"] == ("claude-sonnet-4-6", "claude-haiku-4-5")
        assert loaded["openai"] == ("gpt-5.4",)

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_cached_catalog(path=tmp_path / "absent.json") is None

    def test_load_malformed_returns_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "models.cache.json"
        bad.write_text("not-json", encoding="utf-8")
        assert load_cached_catalog(path=bad) is None

    def test_load_wrong_version_returns_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "models.cache.json"
        bad.write_text(
            json.dumps({"version": 999, "ts": time.time(), "providers": {}}),
            encoding="utf-8",
        )
        assert load_cached_catalog(path=bad) is None

    def test_expired_cache_returns_none(self, tmp_path: Path) -> None:
        expired = tmp_path / "models.cache.json"
        expired.write_text(
            json.dumps(
                {
                    "version": 1,
                    "ts": 0,  # 1970 — definitely older than the TTL
                    "providers": {"anthropic": ["claude-sonnet-4-6"]},
                }
            ),
            encoding="utf-8",
        )
        assert load_cached_catalog(path=expired) is None

    def test_clear_cache(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "models.cache.json"
        save_cached_catalog({"openai": ["gpt-5"]}, path=cache_file)
        assert cache_file.exists()
        assert clear_cached_catalog(path=cache_file) is True
        assert not cache_file.exists()
        # Second clear is a no-op.
        assert clear_cached_catalog(path=cache_file) is False


class TestBedrockCatalogIntegrity:
    """Guard rails on the curated Bedrock model lists.

    Claude 4.x on Bedrock requires a cross-region inference profile
    prefix (us./eu./apac.). The bare ``anthropic.claude-4*`` ids return
    AccessDenied even when model access is granted, so they must NOT
    appear in either Bedrock catalog — otherwise the model picker
    surfaces a guaranteed-failure entry. The SDK has a runtime auto-
    resolver that rewrites bare → regional for users who hand-type
    the bare id, but the catalog itself only lists working ids.
    """

    @pytest.mark.parametrize("provider", ["bedrock", "bedrock_converse"])
    def test_no_bare_claude_4_ids(self, provider: str) -> None:
        models = DEFAULT_MODEL_CANDIDATES[provider]
        offenders = [
            m
            for m in models
            if m.startswith("anthropic.claude-")
            and any(f"claude-{v}-4-" in m for v in ("opus", "sonnet", "haiku"))
        ]
        assert not offenders, (
            f"{provider} catalog contains bare Claude 4.x ids that return "
            f"AccessDenied on Bedrock: {offenders}. Add a regional prefix "
            f"(us./eu./apac.) or drop the entry entirely."
        )

    @pytest.mark.parametrize("provider", ["bedrock", "bedrock_converse"])
    def test_us_inference_profiles_present(self, provider: str) -> None:
        models = DEFAULT_MODEL_CANDIDATES[provider]
        # Every Bedrock catalog must offer at least one us-prefixed
        # Anthropic option so a fresh user on us-east-1 can pick a
        # working model without diagnostics.
        us_anthropic = [m for m in models if m.startswith("us.anthropic.claude-")]
        assert us_anthropic, (
            f"{provider} catalog has no us.anthropic.* entries — a fresh "
            f"user on us-east-1 won't find a working Claude model."
        )


class TestModelDisplayDataclass:
    """Hashability + equality so the picker can use ModelDisplay as a dict key."""

    def test_equality(self) -> None:
        a = derive_model_display("anthropic", "claude-sonnet-4-6")
        b = derive_model_display("anthropic", "claude-sonnet-4-6")
        assert a == b

    def test_is_frozen(self) -> None:
        d = derive_model_display("anthropic", "claude-sonnet-4-6")
        assert isinstance(d, ModelDisplay)
        with pytest.raises(AttributeError):
            # frozen dataclass — write attempts must raise
            d.display_name = "tampered"  # type: ignore[misc]
