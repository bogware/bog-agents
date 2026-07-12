"""Tests for bog_agents._models helpers."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models import BaseChatModel

from bog_agents._models import (
    _apply_openai_responses_default,
    _normalize_bedrock_model_id,
    _normalize_provider,
    _resolve_bedrock_region_prefix,
    _string_value,
    get_model_identifier,
    get_model_provider,
    is_bedrock_model,
    model_matches_spec,
    resolve_model,
)


def _make_model(dump: dict, ls_params: dict | None = None) -> MagicMock:
    """Create a mock BaseChatModel with a given model_dump / _get_ls_params.

    Args:
        dump: Value returned by `model.model_dump()` (drives the identifier).
        ls_params: When provided, the mapping returned by `_get_ls_params()`
            (drives the provider). When omitted, `_get_ls_params` is left as a
            bare MagicMock so `get_model_provider` reports the provider as
            uninspectable, matching a custom model with no LangSmith params.

    Returns:
        A configured mock chat model.
    """
    model = MagicMock(spec=BaseChatModel)
    model.model_dump.return_value = dump
    if ls_params is not None:
        model._get_ls_params.return_value = ls_params
    return model


class TestResolveModel:
    """Tests for resolve_model."""

    def test_passthrough_when_already_model(self) -> None:
        model = MagicMock(spec=BaseChatModel)
        assert resolve_model(model) is model

    def test_openai_prefix_uses_responses_api(self) -> None:
        with patch("bog_agents._models.init_chat_model") as mock:
            mock.return_value = MagicMock(spec=BaseChatModel)
            result = resolve_model("openai:gpt-5")

        # OpenAI also gets the long read timeout (7200s default).
        mock.assert_called_once_with("openai:gpt-5", use_responses_api=True, timeout=7200.0)
        assert result is mock.return_value

    def test_non_openai_string(self) -> None:
        with patch("bog_agents._models.init_chat_model") as mock:
            mock.return_value = MagicMock(spec=BaseChatModel)
            result = resolve_model("anthropic:claude-sonnet-4-6")

        # Anthropic is forwarded with `timeout=` so long turns don't get cut off.
        mock.assert_called_once_with("anthropic:claude-sonnet-4-6", timeout=7200.0)
        assert result is mock.return_value

    def test_timeout_env_override_applied(self, monkeypatch: object) -> None:
        """`BOG_AGENTS_MODEL_READ_TIMEOUT` overrides the default."""
        monkeypatch.setenv("BOG_AGENTS_MODEL_READ_TIMEOUT", "120")  # type: ignore[attr-defined]
        with patch("bog_agents._models.init_chat_model") as mock:
            mock.return_value = MagicMock(spec=BaseChatModel)
            resolve_model("anthropic:claude-sonnet-4-6")
        mock.assert_called_once_with("anthropic:claude-sonnet-4-6", timeout=120.0)

    def test_timeout_env_disable_omits_kwarg(self, monkeypatch: object) -> None:
        """`BOG_AGENTS_MODEL_READ_TIMEOUT=none` skips the timeout kwarg entirely."""
        monkeypatch.setenv("BOG_AGENTS_MODEL_READ_TIMEOUT", "none")  # type: ignore[attr-defined]
        with patch("bog_agents._models.init_chat_model") as mock:
            mock.return_value = MagicMock(spec=BaseChatModel)
            resolve_model("anthropic:claude-sonnet-4-6")
        mock.assert_called_once_with("anthropic:claude-sonnet-4-6")

    def test_provider_rejecting_timeout_kwarg_falls_back_cleanly(self) -> None:
        """A provider that doesn't accept timeout retries without it."""
        with patch("bog_agents._models.init_chat_model") as mock:
            ok_model = MagicMock(spec=BaseChatModel)
            mock.side_effect = [TypeError("unexpected timeout"), ok_model]
            result = resolve_model("exotic:model")

        assert mock.call_count == 2
        assert result is ok_model


class TestGetModelIdentifier:
    """Tests for get_model_identifier."""

    def test_returns_model_name(self) -> None:
        model = _make_model({"model_name": "gpt-5", "model": "something-else"})
        assert get_model_identifier(model) == "gpt-5"

    def test_falls_back_to_model(self) -> None:
        model = _make_model({"model": "claude-sonnet-4-6"})
        assert get_model_identifier(model) == "claude-sonnet-4-6"

    def test_returns_none_when_missing(self) -> None:
        model = _make_model({})
        assert get_model_identifier(model) is None

    def test_skips_empty_model_name(self) -> None:
        model = _make_model({"model_name": "", "model": "fallback"})
        assert get_model_identifier(model) == "fallback"

    def test_skips_non_string_model_name(self) -> None:
        model = _make_model({"model_name": 123, "model": "real-name"})
        assert get_model_identifier(model) == "real-name"


class TestModelMatchesSpec:
    """Tests for model_matches_spec."""

    def test_exact_match(self) -> None:
        model = _make_model({"model_name": "claude-sonnet-4-6"})
        assert model_matches_spec(model, "claude-sonnet-4-6") is True

    def test_provider_prefixed_match(self) -> None:
        model = _make_model({"model_name": "claude-sonnet-4-6"})
        assert model_matches_spec(model, "anthropic:claude-sonnet-4-6") is True

    def test_no_match(self) -> None:
        model = _make_model({"model_name": "claude-sonnet-4-6"})
        assert model_matches_spec(model, "openai:gpt-5") is False

    def test_none_identifier_returns_false(self) -> None:
        model = _make_model({})
        assert model_matches_spec(model, "anything") is False

    def test_bare_spec_without_colon_no_false_positive(self) -> None:
        model = _make_model({"model_name": "gpt-5"})
        assert model_matches_spec(model, "gpt-4o") is False

    def test_cross_provider_same_model_name_no_match(self) -> None:
        # Same model-name half, different provider: the provider guard must
        # reject this. Previously it returned True (provider was dropped).
        model = _make_model({"model_name": "gpt-5"}, ls_params={"ls_provider": "anthropic"})
        assert model_matches_spec(model, "openai:gpt-5") is False

    def test_provider_prefixed_match_with_matching_provider(self) -> None:
        model = _make_model({"model_name": "gpt-5"}, ls_params={"ls_provider": "openai"})
        assert model_matches_spec(model, "openai:gpt-5") is True

    def test_provider_uninspectable_falls_back_to_identifier(self) -> None:
        # No ls_params -> provider uninspectable -> identifier-only fallback.
        model = _make_model({"model_name": "gpt-5"})
        assert model_matches_spec(model, "openai:gpt-5") is True

    def test_provider_alias_normalization_matches(self) -> None:
        # Spec says `mistralai`; ls_provider reports `mistral`. Aliased -> match.
        model = _make_model({"model_name": "mistral-large"}, ls_params={"ls_provider": "mistral"})
        assert model_matches_spec(model, "mistralai:mistral-large") is True

    def test_provider_case_and_hyphen_normalization_matches(self) -> None:
        model = _make_model({"model_name": "codex-mini"}, ls_params={"ls_provider": "openai-codex"})
        assert model_matches_spec(model, "openai_codex:codex-mini") is True


class TestStringValue:
    """Tests for _string_value."""

    def test_present(self) -> None:
        assert _string_value({"key": "val"}, "key") == "val"

    def test_missing(self) -> None:
        assert _string_value({}, "key") is None

    def test_empty(self) -> None:
        assert _string_value({"key": ""}, "key") is None

    def test_non_string(self) -> None:
        assert _string_value({"key": 42}, "key") is None


class TestBedrockRegionPrefix:
    """Tests for _resolve_bedrock_region_prefix."""

    @pytest.mark.parametrize(
        ("region", "expected"),
        [
            ("us-east-1", "us"),
            ("us-east-2", "us"),
            ("us-west-2", "us"),
            ("ca-central-1", "us"),
            ("eu-west-1", "eu"),
            ("eu-central-1", "eu"),
            ("ap-northeast-1", "jp"),
            ("ap-northeast-2", "apac"),
            ("ap-southeast-1", "apac"),
            ("ap-southeast-2", "apac"),
            ("sa-east-1", "sa"),
            ("US-EAST-1", "us"),
            ("", "us"),
            (None, "us"),
            ("af-south-1", "us"),
        ],
    )
    def test_region_to_prefix(self, region: str | None, expected: str) -> None:
        assert _resolve_bedrock_region_prefix(region) == expected


class TestNormalizeBedrockModelId:
    """Tests for _normalize_bedrock_model_id (the AccessDenied trap fix)."""

    def test_non_bedrock_spec_unchanged(self) -> None:
        assert _normalize_bedrock_model_id("anthropic:claude-opus-4-7") == "anthropic:claude-opus-4-7"

    def test_bedrock_already_prefixed_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        spec = "bedrock_converse:us.anthropic.claude-opus-4-7"
        assert _normalize_bedrock_model_id(spec) == spec

    def test_bedrock_nova_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Nova works bare on Bedrock — must not be rewritten.
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        spec = "bedrock_converse:amazon.nova-pro-v1:0"
        assert _normalize_bedrock_model_id(spec) == spec

    def test_bedrock_llama_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        spec = "bedrock_converse:meta.llama4-scout-17b-instruct-v1:0"
        assert _normalize_bedrock_model_id(spec) == spec

    def test_claude_3_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Older Claude 3 IDs still accept on-demand throughput — leave them alone.
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        spec = "bedrock_converse:anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert _normalize_bedrock_model_id(spec) == spec

    def test_claude_4_us_region_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        assert _normalize_bedrock_model_id("bedrock_converse:anthropic.claude-opus-4-7") == "bedrock_converse:us.anthropic.claude-opus-4-7"

    def test_claude_4_eu_region_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert _normalize_bedrock_model_id("bedrock_converse:anthropic.claude-sonnet-4-6") == "bedrock_converse:eu.anthropic.claude-sonnet-4-6"

    def test_claude_4_japan_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        assert _normalize_bedrock_model_id("bedrock_converse:anthropic.claude-opus-4-7") == "bedrock_converse:jp.anthropic.claude-opus-4-7"

    def test_claude_4_apac_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
        assert (
            _normalize_bedrock_model_id("bedrock_converse:anthropic.claude-haiku-4-5-20251001-v1:0")
            == "bedrock_converse:apac.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    def test_no_region_falls_back_to_us(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert _normalize_bedrock_model_id("bedrock_converse:anthropic.claude-opus-4-7") == "bedrock_converse:us.anthropic.claude-opus-4-7"

    def test_aws_default_region_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
        assert _normalize_bedrock_model_id("bedrock_converse:anthropic.claude-opus-4-7") == "bedrock_converse:eu.anthropic.claude-opus-4-7"

    def test_legacy_bedrock_provider_also_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The legacy `bedrock:` (InvokeModel) provider needs the same fix.
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        assert _normalize_bedrock_model_id("bedrock:anthropic.claude-opus-4-7") == "bedrock:us.anthropic.claude-opus-4-7"

    def test_resolve_model_applies_rewrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: resolve_model() passes the rewritten id to init_chat_model."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        with patch("bog_agents._models.init_chat_model") as mock:
            mock.return_value = MagicMock(spec=BaseChatModel)
            resolve_model("bedrock_converse:anthropic.claude-opus-4-7")
        # First positional arg is the rewritten spec.
        call_args = mock.call_args
        assert call_args.args[0] == "bedrock_converse:us.anthropic.claude-opus-4-7"


class TestGetModelProvider:
    """Tests for get_model_provider (with the tightened except)."""

    def test_returns_provider_from_ls_params(self) -> None:
        model = _make_model({"model_name": "gpt-5"}, ls_params={"ls_provider": "openai"})
        assert get_model_provider(model) == "openai"

    def test_non_mapping_ls_params_returns_none(self) -> None:
        model = MagicMock(spec=BaseChatModel)
        model._get_ls_params.return_value = "not-a-mapping"
        assert get_model_provider(model) is None

    def test_missing_provider_key_returns_none(self) -> None:
        model = _make_model({"model_name": "x"}, ls_params={})
        assert get_model_provider(model) is None

    def test_raising_ls_params_returns_none(self) -> None:
        model = MagicMock(spec=BaseChatModel)
        model._get_ls_params.side_effect = NotImplementedError
        assert get_model_provider(model) is None

    def test_unexpected_exception_propagates(self) -> None:
        # The bare `except` was narrowed: an unrelated error is no longer
        # silently mapped to `None`.
        model = MagicMock(spec=BaseChatModel)
        model._get_ls_params.side_effect = KeyError("boom")
        with pytest.raises(KeyError):
            get_model_provider(model)


class TestIsBedrockModel:
    """Tests for is_bedrock_model."""

    @pytest.mark.parametrize(
        "spec",
        [
            "bedrock:anthropic.claude-opus-4-7",
            "bedrock_converse:us.anthropic.claude-opus-4-7",
            "aws:anthropic.claude-sonnet-4-6",
            "amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
        ],
    )
    def test_bedrock_specs(self, spec: str) -> None:
        assert is_bedrock_model(spec) is True

    @pytest.mark.parametrize(
        "spec",
        [
            "openai:gpt-5",
            "anthropic:claude-sonnet-4-6",
            "google_genai:gemini-2.5-pro",
            "gpt-5",
        ],
    )
    def test_non_bedrock_specs(self, spec: str) -> None:
        assert is_bedrock_model(spec) is False

    def test_instance_provider_detects_bedrock(self) -> None:
        model = _make_model({"model_name": "x"}, ls_params={"ls_provider": "bedrock_converse"})
        assert is_bedrock_model(model) is True

    def test_instance_class_name_detects_bedrock(self) -> None:
        class ChatBedrockConverse:
            def _get_ls_params(self) -> dict:
                return {}

        assert is_bedrock_model(ChatBedrockConverse()) is True  # type: ignore[arg-type]

    def test_instance_non_bedrock(self) -> None:
        model = _make_model({"model_name": "gpt-5"}, ls_params={"ls_provider": "openai"})
        assert is_bedrock_model(model) is False


class TestNormalizeProvider:
    """Tests for _normalize_provider."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("openai", "openai"),
            ("OpenAI", "openai"),
            ("openai-codex", "openai_codex"),
            ("azure_openai", "azure"),
            ("mistralai", "mistral"),
            ("Mistralai", "mistral"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert _normalize_provider(raw) == expected


class TestApplyOpenaiResponsesDefault:
    """Tests for _apply_openai_responses_default (the overridable default)."""

    def test_openai_gets_default_when_absent(self) -> None:
        kwargs: dict = {}
        _apply_openai_responses_default("openai:gpt-5", kwargs)
        assert kwargs == {"use_responses_api": True}

    def test_non_openai_untouched(self) -> None:
        kwargs: dict = {}
        _apply_openai_responses_default("anthropic:claude-sonnet-4-6", kwargs)
        assert kwargs == {}

    def test_existing_value_not_overridden(self) -> None:
        # A profile (or caller) already decided -> the default must not clobber.
        kwargs = {"use_responses_api": False}
        _apply_openai_responses_default("openai:gpt-5", kwargs)
        assert kwargs == {"use_responses_api": False}


class TestResponsesApiOverridableEndToEnd:
    """resolve_model must let a profile control use_responses_api for OpenAI."""

    def test_profile_can_disable_responses_api(self) -> None:
        # Simulate a ProviderProfile that sets use_responses_api=False by having
        # apply_provider_profile return it. The post-profile default must then
        # leave it alone (proving the override is no longer dead).
        def fake_apply(spec: str, kwargs: dict) -> dict:
            merged = dict(kwargs)
            merged["use_responses_api"] = False
            return merged

        with (
            patch("bog_agents.profiles.provider.provider_profiles.apply_provider_profile", side_effect=fake_apply),
            patch("bog_agents._models.init_chat_model") as mock,
        ):
            mock.return_value = MagicMock(spec=BaseChatModel)
            resolve_model("openai:gpt-5")

        assert mock.call_args.kwargs.get("use_responses_api") is False

    def test_default_responses_api_when_no_profile(self) -> None:
        # No profile touches the key -> OpenAI still defaults to the Responses
        # API (behavior preserved for today's users).
        with patch("bog_agents._models.init_chat_model") as mock:
            mock.return_value = MagicMock(spec=BaseChatModel)
            resolve_model("openai:gpt-5")

        assert mock.call_args.kwargs.get("use_responses_api") is True
