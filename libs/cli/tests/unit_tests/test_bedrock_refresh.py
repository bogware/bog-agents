"""Tests for the Bedrock credential auto-refresh middleware (Wave 4)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli.bedrock_refresh import (
    BedrockRefreshMiddleware,
    _attempt_sso_login,
    _reset_session_state_for_tests,
    _resolved_profile,
)


class _ExpiredTokenException(Exception):  # noqa: N818 — boto3 names its real class this way; mimic that
    """Stand-in for the boto exception class — avoids importing boto in tests."""


@pytest.fixture(autouse=True)
def _clean_session() -> None:
    """Reset module-level counters between tests."""
    _reset_session_state_for_tests()
    yield
    _reset_session_state_for_tests()


class TestResolvedProfile:
    """Profile resolution honors the documented precedence."""

    def test_bog_profile_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_PROFILE", "shared-aws")
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_PROFILE", "bog-only")
        assert _resolved_profile() == "bog-only"

    def test_aws_profile_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOG_AGENTS_BEDROCK_PROFILE", raising=False)
        monkeypatch.setenv("AWS_PROFILE", "shared-aws")
        assert _resolved_profile() == "shared-aws"

    def test_default_when_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOG_AGENTS_BEDROCK_PROFILE", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        assert _resolved_profile() == "default"


class TestAttemptSsoLogin:
    """``_attempt_sso_login`` runs the right command + reports success."""

    def test_runs_aws_sso_login_with_profile(self) -> None:
        with patch(
            "bog_agents_cli.bedrock_refresh.shutil.which", return_value="/usr/bin/aws"
        ):
            with patch("bog_agents_cli.bedrock_refresh.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                ok = _attempt_sso_login("my-profile")
        assert ok is True
        # Argv must contain the profile we passed.
        args = mock_run.call_args.args[0]
        assert args[:3] == ["aws", "sso", "login"]
        assert "--profile" in args
        assert args[args.index("--profile") + 1] == "my-profile"

    def test_returns_false_when_aws_cli_missing(self) -> None:
        with patch("bog_agents_cli.bedrock_refresh.shutil.which", return_value=None):
            assert _attempt_sso_login("anything") is False

    def test_returns_false_on_non_zero_exit(self) -> None:
        with patch(
            "bog_agents_cli.bedrock_refresh.shutil.which", return_value="/usr/bin/aws"
        ):
            with patch("bog_agents_cli.bedrock_refresh.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                assert _attempt_sso_login("p") is False

    def test_returns_false_on_timeout(self) -> None:
        import subprocess as _subprocess

        with patch(
            "bog_agents_cli.bedrock_refresh.shutil.which", return_value="/usr/bin/aws"
        ):
            with patch("bog_agents_cli.bedrock_refresh.subprocess.run") as mock_run:
                mock_run.side_effect = _subprocess.TimeoutExpired(
                    cmd="aws", timeout=120
                )
                assert _attempt_sso_login("p") is False


class TestRefreshHits:
    """``_should_attempt_refresh`` classifies the right errors."""

    def test_expired_token_triggers_refresh(self) -> None:
        mw = BedrockRefreshMiddleware()
        exc = Exception("The security token included in the request is expired")
        assert mw._should_attempt_refresh(exc) is True

    def test_generic_5xx_does_not_trigger(self) -> None:
        mw = BedrockRefreshMiddleware()
        exc = RuntimeError("InternalServerError: 500")
        assert mw._should_attempt_refresh(exc) is False

    def test_missing_creds_does_not_trigger(self) -> None:
        # Missing creds is a different category — print the banner via
        # the normal error path, don't try to refresh.
        mw = BedrockRefreshMiddleware()
        exc = Exception("Unable to locate credentials")
        assert mw._should_attempt_refresh(exc) is False

    def test_access_denied_does_not_trigger(self) -> None:
        mw = BedrockRefreshMiddleware()
        exc = Exception("An error occurred (AccessDeniedException)")
        assert mw._should_attempt_refresh(exc) is False


class TestRefreshFlow:
    """End-to-end refresh + retry, with subprocess mocked."""

    def test_sync_path_refreshes_and_retries(self) -> None:
        mw = BedrockRefreshMiddleware(interactive=True)
        call_next = MagicMock(
            side_effect=[
                Exception("ExpiredTokenException: token is expired"),
                "RECOVERED",
            ]
        )
        with patch(
            "bog_agents_cli.bedrock_refresh._attempt_sso_login", return_value=True
        ):
            result = mw.wrap_model_call(
                request=MagicMock(), call_next=call_next
            )
        assert result == "RECOVERED"
        assert call_next.call_count == 2

    def test_sync_path_no_refresh_when_subprocess_fails(self) -> None:
        mw = BedrockRefreshMiddleware(interactive=True)
        original = Exception("ExpiredTokenException: token is expired")
        call_next = MagicMock(side_effect=original)
        with patch(
            "bog_agents_cli.bedrock_refresh._attempt_sso_login", return_value=False
        ):
            with pytest.raises(Exception, match="ExpiredTokenException"):
                mw.wrap_model_call(
                    request=MagicMock(), call_next=call_next
                )
        # No retry attempted when subprocess failed.
        assert call_next.call_count == 1

    def test_headless_mode_does_not_run_subprocess(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mw = BedrockRefreshMiddleware(interactive=False)
        original = Exception("ExpiredTokenException: token is expired")
        call_next = MagicMock(side_effect=original)
        with patch("bog_agents_cli.bedrock_refresh._attempt_sso_login") as mock_login:
            with pytest.raises(Exception, match="ExpiredTokenException"):
                mw.wrap_model_call(
                    request=MagicMock(), call_next=call_next
                )
            # Headless mode must NEVER spawn the subprocess.
            mock_login.assert_not_called()
        # But it must print the actionable banner to stderr.
        captured = capsys.readouterr()
        assert "aws sso login" in captured.err
        assert "BEDROCK" in captured.err

    def test_session_budget_caps_refreshes(self) -> None:
        # Three refreshes allowed, fourth must NOT spawn subprocess.
        mw = BedrockRefreshMiddleware(interactive=True, max_refreshes_per_session=3)
        expired = Exception("ExpiredTokenException: token is expired")
        with patch(
            "bog_agents_cli.bedrock_refresh._attempt_sso_login", return_value=True
        ) as mock_login:
            for _ in range(3):
                call_next = MagicMock(side_effect=[expired, "OK"])
                result = mw.wrap_model_call(
                    request=MagicMock(), call_next=call_next
                )
                assert result == "OK"
            # First three calls each spawned the subprocess.
            assert mock_login.call_count == 3
            # Fourth: budget exhausted, must NOT spawn, must re-raise.
            call_next = MagicMock(side_effect=expired)
            with pytest.raises(Exception, match="ExpiredTokenException"):
                mw.wrap_model_call(
                    request=MagicMock(), call_next=call_next
                )
            # Subprocess count unchanged from the third refresh.
            assert mock_login.call_count == 3

    def test_first_call_prompts_banner(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # First interactive refresh of the session should print a
        # banner so the user knows a browser tab is about to open.
        mw = BedrockRefreshMiddleware(interactive=True)
        call_next = MagicMock(
            side_effect=[
                Exception("ExpiredTokenException"),
                "OK",
            ]
        )
        with patch(
            "bog_agents_cli.bedrock_refresh._attempt_sso_login", return_value=True
        ):
            mw.wrap_model_call(
                request=MagicMock(), call_next=call_next
            )
        captured = capsys.readouterr()
        assert "BEDROCK" in captured.err
        assert "aws sso login" in captured.err

    def test_keyboard_interrupt_propagates(self) -> None:
        # KeyboardInterrupt must NEVER be swallowed by the refresh path.
        mw = BedrockRefreshMiddleware(interactive=True)
        call_next = MagicMock(side_effect=KeyboardInterrupt)
        with pytest.raises(KeyboardInterrupt):
            mw.wrap_model_call(
                request=MagicMock(), call_next=call_next
            )

    def test_non_credential_error_passes_through(self) -> None:
        # Unrelated errors must propagate unchanged — no refresh, no retry.
        mw = BedrockRefreshMiddleware(interactive=True)
        original = RuntimeError("ServiceUnavailable: 503")
        call_next = MagicMock(side_effect=original)
        with patch("bog_agents_cli.bedrock_refresh._attempt_sso_login") as mock_login:
            with pytest.raises(RuntimeError, match="ServiceUnavailable"):
                mw.wrap_model_call(
                    request=MagicMock(), call_next=call_next
                )
            mock_login.assert_not_called()
        # Single attempt — no retry on non-credential errors.
        assert call_next.call_count == 1


class TestAsyncRefreshFlow:
    """Async path mirrors the sync path."""

    async def test_async_refreshes_and_retries(self) -> None:
        mw = BedrockRefreshMiddleware(interactive=True)

        call_count = 0

        async def call_next(request: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _ExpiredTokenException("ExpiredTokenException: token expired")
            return "RECOVERED"

        with patch(
            "bog_agents_cli.bedrock_refresh._attempt_sso_login", return_value=True
        ):
            result = await mw.awrap_model_call(
                request=MagicMock(), call_next=call_next
            )
        assert result == "RECOVERED"
        assert call_count == 2

    async def test_async_propagates_when_refresh_fails(self) -> None:
        mw = BedrockRefreshMiddleware(interactive=True)

        async def call_next(request: object) -> str:
            raise _ExpiredTokenException("ExpiredTokenException: token expired")

        with patch(
            "bog_agents_cli.bedrock_refresh._attempt_sso_login", return_value=False
        ):
            with pytest.raises(Exception, match="ExpiredTokenException"):
                await mw.awrap_model_call(
                    request=MagicMock(), call_next=call_next
                )
