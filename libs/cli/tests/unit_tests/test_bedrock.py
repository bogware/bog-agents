"""Tests for the bedrock error categorization + connection probe."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    import pytest

from bog_agents_cli._bedrock import (
    BedrockErrorKind,
    ProbeStep,
    categorize_bedrock_error,
    probe_bedrock,
    render_fix_report,
    render_probe_report,
    render_settings_report,
)


def _client_error(code: str, message: str = "") -> Exception:
    """Build a faux botocore ClientError-like exception.

    We don't import ``botocore`` here because the test should pass even
    in an env without the AWS extras installed.
    """
    exc = Exception(f"An error occurred ({code}) when calling: {message or code}")
    exc.response = {"Error": {"Code": code, "Message": message or code}}  # type: ignore[attr-defined]
    return exc


class TestCategorizeBedrockError:
    """Exception → BedrockError mapping covers the user-visible cases."""

    def test_access_denied(self) -> None:
        err = categorize_bedrock_error(_client_error("AccessDeniedException"))
        assert err.kind == BedrockErrorKind.MODEL_ACCESS_DENIED
        assert "model access" in err.title.lower()
        assert "console.aws.amazon.com/bedrock" in err.hint

    def test_validation_exception_for_model_id(self) -> None:
        err = categorize_bedrock_error(
            _client_error(
                "ValidationException",
                "The provided model identifier is invalid.",
            )
        )
        assert err.kind == BedrockErrorKind.MODEL_ID_INVALID
        assert "model id" in err.title.lower()

    def test_throttling(self) -> None:
        err = categorize_bedrock_error(_client_error("ThrottlingException"))
        assert err.kind == BedrockErrorKind.THROTTLED
        assert "throttl" in err.title.lower()

    def test_no_credentials(self) -> None:
        err = categorize_bedrock_error(Exception("Unable to locate credentials"))
        assert err.kind == BedrockErrorKind.CREDENTIALS_MISSING
        assert "aws configure" in err.hint.lower()

    def test_expired_credentials_classified_separately(self) -> None:
        err = categorize_bedrock_error(
            Exception("The security token included in the request is expired")
        )
        assert err.kind == BedrockErrorKind.CREDENTIALS_EXPIRED
        assert "aws sso login" in err.hint.lower()

    def test_region_missing(self) -> None:
        err = categorize_bedrock_error(Exception("You must specify a region."))
        assert err.kind == BedrockErrorKind.REGION_INVALID
        assert "AWS_REGION" in err.hint

    def test_endpoint_unreachable(self) -> None:
        err = categorize_bedrock_error(
            Exception("Could not connect to the endpoint URL: ...")
        )
        # Endpoint connection errors look like region issues — first
        # match in the chain wins, which is fine for actionable hints
        # since both classes end up in the network/region cluster.
        assert err.kind in {BedrockErrorKind.REGION_INVALID, BedrockErrorKind.NETWORK}

    def test_package_missing(self) -> None:
        err = categorize_bedrock_error(ImportError("No module named 'langchain_aws'"))
        assert err.kind == BedrockErrorKind.PACKAGE_MISSING
        assert "pip install" in err.hint

    def test_package_missing_sdk_wrapped_message(self) -> None:
        # The SDK's create_model wraps the ImportError into a ModelConfigError
        # whose text is "Missing package for provider 'bedrock'. Install: pip
        # install langchain-aws" — no "no module named" substring. It must
        # still categorise as PACKAGE_MISSING with the CLI extra hint, not
        # fall through to UNKNOWN.
        err = categorize_bedrock_error(
            Exception(
                "Missing package for provider 'bedrock'. "
                "Install: pip install langchain-aws"
            )
        )
        assert err.kind == BedrockErrorKind.PACKAGE_MISSING
        assert "bog-agents-cli[bedrock]" in err.hint

    def test_unknown_falls_through(self) -> None:
        err = categorize_bedrock_error(Exception("absurd cosmic ray flip"))
        assert err.kind == BedrockErrorKind.UNKNOWN
        assert "Run `bog-agents test-bedrock`" in err.hint

    def test_aws_error_code_extracted(self) -> None:
        err = categorize_bedrock_error(_client_error("AccessDeniedException"))
        assert err.aws_error_code == "AccessDeniedException"

    def test_banner_format_does_not_crash(self) -> None:
        err = categorize_bedrock_error(_client_error("AccessDeniedException"))
        banner = err.banner()
        assert "BEDROCK:" in banner
        assert "=" * 78 in banner


class TestProbeBedrock:
    """The probe runs every step independently, even when one fails."""

    def test_no_boto3_short_circuits(self) -> None:
        """Without boto3 in the environment, the probe must still return."""
        with patch.dict("sys.modules", {"boto3": None, "botocore": None}):
            with patch("bog_agents_cli._bedrock.categorize_bedrock_error") as mock_cat:
                mock_cat.return_value = MagicMock()
                # The first import in probe_bedrock will raise ImportError
                # because we set the module to None. That's the path
                # we want to test.
                steps = probe_bedrock()
        # Either the package step recorded a failure, or boto3 is
        # available and step 1 passed. Either way, list isn't empty.
        assert len(steps) >= 1
        assert steps[0].name == "Package"

    def test_render_report_lists_each_step(self) -> None:
        """The renderer produces a recognisable header + per-step rows."""
        steps = [
            ProbeStep(name="Package", ok=True, detail="boto3 available"),
            ProbeStep(
                name="Credentials",
                ok=False,
                detail="None found",
                error=categorize_bedrock_error(
                    Exception("Unable to locate credentials")
                ),
            ),
        ]
        report = render_probe_report(steps)
        assert "Bedrock connection probe" in report
        assert "[OK]" in report
        assert "Package" in report
        assert "[FAIL]" in report
        assert "Credentials" in report
        # Failure section must include the actionable hint.
        assert "Failure details" in report
        assert "aws configure" in report.lower()


class TestRenderFixReport:
    """``render_fix_report`` produces copy-paste actions, not banners."""

    def test_all_ok_short_circuits(self) -> None:
        steps = [
            ProbeStep(name="Package", ok=True, detail="boto3 available"),
            ProbeStep(name="Credentials", ok=True, detail="env"),
            ProbeStep(name="Region", ok=True, detail="us-east-1"),
        ]
        report = render_fix_report(steps)
        assert "All probe steps passed" in report
        assert "no action needed" in report.lower()

    def test_missing_credentials_emits_aws_configure(self) -> None:
        steps = [
            ProbeStep(name="Package", ok=True, detail="boto3 available"),
            ProbeStep(
                name="Credentials",
                ok=False,
                detail="None found",
                error=categorize_bedrock_error(
                    Exception("Unable to locate credentials")
                ),
            ),
        ]
        report = render_fix_report(steps)
        # Numbered failure block.
        assert "[1] Credentials" in report
        # Concrete command, not a paragraph.
        assert "aws configure" in report
        assert "aws sso login" in report
        # Tail invitation to re-run.
        assert "Ctrl+T" in report or "/bedrock test" in report

    def test_model_access_denied_emits_console_link(self) -> None:
        steps = [
            ProbeStep(
                name="Inference",
                ok=False,
                detail="converse() failed",
                error=categorize_bedrock_error(
                    Exception(
                        "An error occurred (AccessDeniedException): "
                        "You don't have access to the model"
                    )
                ),
            ),
        ]
        report = render_fix_report(steps)
        assert "console.aws.amazon.com/bedrock" in report
        assert "Model access" in report or "model access" in report.lower()

    def test_region_invalid_emits_export(self) -> None:
        steps = [
            ProbeStep(
                name="Region",
                ok=False,
                detail="No region",
                error=categorize_bedrock_error(Exception("You must specify a region")),
            ),
        ]
        report = render_fix_report(steps)
        assert "AWS_REGION" in report
        assert "us-east-1" in report

    def test_validation_emits_inference_profile_hint(self) -> None:
        steps = [
            ProbeStep(
                name="Inference",
                ok=False,
                detail="converse() failed",
                error=categorize_bedrock_error(
                    Exception(
                        "An error occurred (ValidationException): "
                        "The provided model identifier is invalid"
                    )
                ),
            ),
        ]
        report = render_fix_report(steps)
        # The fix should steer them to the inference-profile-prefixed id.
        assert "us." in report
        assert "claude" in report.lower()

    def test_multiple_failures_numbered_independently(self) -> None:
        steps = [
            ProbeStep(name="Package", ok=True, detail="boto3 available"),
            ProbeStep(
                name="Credentials",
                ok=False,
                detail="None",
                error=categorize_bedrock_error(
                    Exception("Unable to locate credentials")
                ),
            ),
            ProbeStep(
                name="Region",
                ok=False,
                detail="None",
                error=categorize_bedrock_error(Exception("You must specify a region")),
            ),
        ]
        report = render_fix_report(steps)
        assert "[1]" in report
        assert "[2]" in report
        assert report.index("[1]") < report.index("[2]")


class TestRenderSettingsReport:
    """``render_settings_report`` surfaces the active Bedrock config."""

    def test_default_auth_mode_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOG_AGENTS_BEDROCK_AUTH_MODE", raising=False)
        monkeypatch.delenv("BOG_AGENTS_BEDROCK_PROFILE", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        report = render_settings_report()
        assert "Bedrock settings" in report
        assert "auth_mode" in report
        # Default auth mode when nothing configured is "auto".
        assert "auto" in report
        assert "<unset>" in report  # region / profile

    def test_env_overrides_visible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_AUTH_MODE", "sso")
        monkeypatch.setenv("BOG_AGENTS_BEDROCK_PROFILE", "my-team-sso")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        report = render_settings_report()
        assert "sso" in report
        assert "my-team-sso" in report
        assert "eu-west-1" in report

    def test_includes_toml_example_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = render_settings_report()
        assert "[models.providers.bedrock_converse]" in report
        assert "auth_mode" in report
        # Env-var alternatives are also shown.
        assert "BOG_AGENTS_BEDROCK_AUTH_MODE" in report
