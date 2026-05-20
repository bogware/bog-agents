"""Tests for bog_agents._models helpers."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models import BaseChatModel

from bog_agents._models import (
    _normalize_bedrock_model_id,
    _resolve_bedrock_region_prefix,
    _string_value,
    get_model_identifier,
    model_matches_spec,
    resolve_model,
)


def _make_model(dump: dict) -> MagicMock:
    """Create a mock BaseChatModel with a given model_dump return."""
    model = MagicMock(spec=BaseChatModel)
    model.model_dump.return_value = dump
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
