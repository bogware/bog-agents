"""Tests for the Bedrock credential-detection + auth-mode chain.

These were uncovered until the Bedrock-seamless wave — the SSO →
static-creds fallback and the env-vs-config-vs-default precedence
for ``_bedrock_auth_mode`` had no regression guards.

All mocks. No live AWS calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bog_agents_cli.model_config import (
    _bedrock_auth_mode,
    _check_bedrock_boto3,
    _check_bedrock_files,
)


def _clear_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every AWS-related env var so each test starts from zero."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "BOG_AGENTS_BEDROCK_AUTH_MODE",
        "BOG_AGENTS_BEDROCK_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)


class TestCheckBedrockFiles:
    """``_check_bedrock_files`` — fast existence check, no boto3."""

    def test_no_creds_anywhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_aws_env(monkeypatch)
        # Point Path.home() at a fresh temp dir so ~/.aws/credentials
        # doesn't leak in from the developer's real home.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _check_bedrock_files() is False

    def test_env_access_key_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA...")
        assert _check_bedrock_files() is True

    def test_aws_profile_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("AWS_PROFILE", "my-profile")
        assert _check_bedrock_files() is True

    def test_credentials_file_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        aws_dir = tmp_path / ".aws"
        aws_dir.mkdir()
        (aws_dir / "credentials").write_text("[default]\n", encoding="utf-8")
        assert _check_bedrock_files() is True

    def test_sso_cache_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        sso_cache = tmp_path / ".aws" / "sso" / "cache"
        sso_cache.mkdir(parents=True)
        (sso_cache / "abc.json").write_text("{}", encoding="utf-8")
        assert _check_bedrock_files() is True

    def test_web_identity_token_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # EKS / GitHub Actions OIDC path.
        _clear_aws_env(monkeypatch)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(tmp_path / "token.jwt"))
        assert _check_bedrock_files() is True


class TestCheckBedrockBoto3:
    """``_check_bedrock_boto3`` — delegate to boto3.Session.get_credentials."""

    def test_creds_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock boto3.Session to return a session whose get_credentials
        # returns a non-None Credentials placeholder.
        class _FakeCreds:
            pass

        class _FakeSession:
            def get_credentials(self) -> object:
                return _FakeCreds()

        fake_boto3 = type("FakeBoto3", (), {"Session": _FakeSession})()
        with patch.dict("sys.modules", {"boto3": fake_boto3}):
            assert _check_bedrock_boto3() is True

    def test_no_creds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeSession:
            def get_credentials(self) -> None:
                return None

        fake_boto3 = type("FakeBoto3", (), {"Session": _FakeSession})()
        with patch.dict("sys.modules", {"boto3": fake_boto3}):
            assert _check_bedrock_boto3() is False

    def test_session_raises_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeSession:
            def get_credentials(self) -> None:
                raise RuntimeError("expired")

        fake_boto3 = type("FakeBoto3", (), {"Session": _FakeSession})()
        with patch.dict("sys.modules", {"boto3": fake_boto3}):
            assert _check_bedrock_boto3() is False


class TestBedrockAuthMode:
    """``_bedrock_auth_mode`` precedence: env > config > default."""

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_AUTH_MODE", "sso")
        mode, _ = _bedrock_auth_mode()
        assert mode == "sso"

    def test_env_profile_overrides_aws_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setenv("AWS_PROFILE", "default-aws-profile")
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_PROFILE", "bog-team")
        _, profile = _bedrock_auth_mode()
        assert profile == "bog-team"

    def test_aws_profile_fallback_when_no_bog_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setenv("AWS_PROFILE", "my-aws-prof")
        _, profile = _bedrock_auth_mode()
        assert profile == "my-aws-prof"

    def test_default_is_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_aws_env(monkeypatch)
        mode, profile = _bedrock_auth_mode()
        assert mode == "auto"
        assert profile == ""

    def test_unknown_mode_falls_back_to_auto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_AUTH_MODE", "telepathy")
        mode, _ = _bedrock_auth_mode()
        assert mode == "auto"

    @pytest.mark.parametrize("valid", ["auto", "sso", "static", "profile", "iam"])
    def test_all_valid_modes_pass_through(
        self, monkeypatch: pytest.MonkeyPatch, valid: str
    ) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_AUTH_MODE", valid)
        mode, _ = _bedrock_auth_mode()
        assert mode == valid

    def test_env_mode_is_case_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_aws_env(monkeypatch)
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_AUTH_MODE", "  SSO  ")
        mode, _ = _bedrock_auth_mode()
        assert mode == "sso"
