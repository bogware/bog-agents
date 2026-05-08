"""Tests for the bedrock error categorization + connection probe."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bog_agents_cli._bedrock import (
    BedrockErrorKind,
    categorize_bedrock_error,
    probe_bedrock,
    render_probe_report,
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
        from bog_agents_cli._bedrock import ProbeStep

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
