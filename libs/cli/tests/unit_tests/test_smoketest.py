"""Tests for `bog_agents_cli.smoketest`.

Covers the error categoriser, the spec parser, and the public
``smoketest_model`` shape. We do **not** make real network calls in
unit tests — exceptions are injected via monkeypatched ``create_model``.
"""

from __future__ import annotations

import pytest

from bog_agents_cli import smoketest as smoketest_module
from bog_agents_cli.smoketest import (
    SmoketestKind,
    SmoketestResult,
    smoketest_model,
)


class TestCategorize:
    """`_categorize` correctly maps common failure shapes."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("401 Unauthorized", SmoketestKind.AUTH_INVALID),
            ("Could not load credentials", SmoketestKind.AUTH_MISSING),
            ("Unable to locate credentials", SmoketestKind.AUTH_MISSING),
            ("Rate limit exceeded — try again later", SmoketestKind.QUOTA),
            ("ThrottlingException: too many requests", SmoketestKind.QUOTA),
            ("Connection refused", SmoketestKind.NETWORK),
            ("DNS resolution failed", SmoketestKind.NETWORK),
            ("Model not found", SmoketestKind.MODEL_NOT_FOUND),
            ("ValidationException: bad modelId", SmoketestKind.MODEL_NOT_FOUND),
            ("Some weird error from outer space", SmoketestKind.UNKNOWN),
        ],
    )
    def test_signal_mapping(self, message: str, expected: SmoketestKind) -> None:
        kind, _ = smoketest_module._categorize(Exception(message))
        assert kind is expected

    def test_timeout_error_class(self) -> None:
        kind, _ = smoketest_module._categorize(TimeoutError("timed out"))
        assert kind is SmoketestKind.TIMEOUT

    def test_import_error(self) -> None:
        kind, _ = smoketest_module._categorize(ImportError("No module named 'foo'"))
        assert kind is SmoketestKind.PACKAGE_MISSING


class TestHintFor:
    """`_hint_for` returns provider-aware action steps."""

    def test_anthropic_missing_creds_mentions_env_var(self) -> None:
        hint = smoketest_module._hint_for(SmoketestKind.AUTH_MISSING, "anthropic")
        assert "ANTHROPIC_API_KEY" in hint

    def test_bedrock_missing_creds_mentions_aws(self) -> None:
        hint = smoketest_module._hint_for(
            SmoketestKind.AUTH_MISSING, "bedrock_converse"
        )
        assert "aws" in hint.lower()

    def test_bedrock_model_not_found_mentions_inference_profile(self) -> None:
        hint = smoketest_module._hint_for(
            SmoketestKind.MODEL_NOT_FOUND, "bedrock_converse"
        )
        assert "inference profile" in hint.lower()

    def test_quota_hint(self) -> None:
        hint = smoketest_module._hint_for(SmoketestKind.QUOTA, "anthropic")
        assert "retry" in hint.lower() or "quota" in hint.lower()


class TestSmoketestModelShape:
    """Public entry returns a `SmoketestResult` for both happy and sad paths."""

    def test_invalid_spec_returns_unknown_result(self) -> None:
        # No provider, no detectable model — should fail gracefully.
        result = smoketest_model("")
        assert isinstance(result, SmoketestResult)
        assert result.kind is SmoketestKind.UNKNOWN
        # Should not crash, should not propagate.

    def test_auth_failure_categorised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When `create_model` raises an auth error, kind=AUTH_INVALID."""

        def fake_create_model(spec: str, **_kw: object) -> object:
            msg = "401 Unauthorized: bad api key"
            raise RuntimeError(msg)

        # Patch the *import target* — `smoketest_model` does
        # `from bog_agents_cli.config import create_model` inside the
        # function, so patching the source module is what matters.
        monkeypatch.setattr("bog_agents_cli.config.create_model", fake_create_model)
        result = smoketest_model("anthropic:claude-haiku-4-5")
        assert result.kind is SmoketestKind.AUTH_INVALID
        assert "ANTHROPIC_API_KEY" in result.hint or "valid" in result.hint.lower()
        assert result.ok is False

    def test_model_not_found_categorised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_create_model(spec: str, **_kw: object) -> object:
            msg = "Model not found: claude-nonexistent"
            raise RuntimeError(msg)

        monkeypatch.setattr("bog_agents_cli.config.create_model", fake_create_model)
        result = smoketest_model("anthropic:claude-nonexistent")
        assert result.kind is SmoketestKind.MODEL_NOT_FOUND
        assert result.ok is False

    def test_summary_markup_format(self) -> None:
        result = SmoketestResult(
            spec="anthropic:claude-haiku-4-5",
            kind=SmoketestKind.OK,
            elapsed_seconds=0.42,
            message="ok",
        )
        markup = result.summary_markup()
        assert "PASS" in markup
        assert "0.4s" in markup

    def test_summary_markup_failure(self) -> None:
        result = SmoketestResult(
            spec="anthropic:claude-haiku-4-5",
            kind=SmoketestKind.AUTH_INVALID,
            elapsed_seconds=0.1,
            message="creds rejected",
        )
        markup = result.summary_markup()
        assert "FAIL" in markup
        assert "creds rejected" in markup

    def test_thinking_marker_in_summary(self) -> None:
        result = SmoketestResult(
            spec="anthropic:claude-sonnet-4-6",
            kind=SmoketestKind.OK,
            elapsed_seconds=1.0,
            message="ok",
            thinking_used=True,
        )
        assert "thinking" in result.summary_markup()
