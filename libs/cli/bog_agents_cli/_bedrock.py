"""AWS Bedrock helpers: error categorization + connection diagnostics.

Bedrock failures from `boto3` / `langchain-aws` come back as opaque
`ClientError` / `BotoCoreError` instances with a stack of
`{Error: {Code: ..., Message: ...}}` envelopes. New users — and even
experienced ones in unfamiliar accounts — see the raw stack trace and
have no idea whether they need to:

- `aws configure` (no credentials at all)
- `aws sso login` (expired credentials)
- request model access in the Bedrock console (most common)
- switch region (model not available everywhere)
- pick a different model id (sonnet vs sonnet-converse)

This module centralises the diagnosis. ``categorize_bedrock_error``
turns any boto/Bedrock exception into a structured ``BedrockError``
with a human-readable hint; ``test_bedrock_connection`` runs a
multi-step probe (creds → region → list-models → tiny inference) and
reports each step with a clear pass/fail icon. Both the slash command
and the CLI subcommand re-use the same probe so behavior stays
consistent across surfaces.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error categorization
# ---------------------------------------------------------------------------


class BedrockErrorKind(StrEnum):
    """High-level categories the user can act on.

    The point isn't a complete taxonomy — it's the **action** each
    category implies. ``CREDENTIALS_MISSING`` means run ``aws configure``;
    ``MODEL_ACCESS_DENIED`` means open the console.
    """

    CREDENTIALS_MISSING = "credentials_missing"
    """No credentials at all — boto3 chain returned empty."""

    CREDENTIALS_EXPIRED = "credentials_expired"
    """SSO/STS token expired; user needs to re-login."""

    MODEL_ACCESS_DENIED = "model_access_denied"
    """User must request model access in the Bedrock console."""

    REGION_INVALID = "region_invalid"
    """Region missing or model not available in selected region."""

    MODEL_ID_INVALID = "model_id_invalid"
    """Bedrock returned ValidationException on the modelId."""

    THROTTLED = "throttled"
    """ThrottlingException — back off and retry."""

    NETWORK = "network"
    """DNS / connection / TLS — connectivity issue, not auth."""

    PACKAGE_MISSING = "package_missing"
    """``langchain-aws`` or ``boto3`` not installed."""

    UNKNOWN = "unknown"
    """Couldn't categorise — surface raw error + full traceback."""


@dataclass(frozen=True)
class BedrockError:
    """Structured Bedrock failure with a user-actionable hint.

    Attributes:
        kind: One of :class:`BedrockErrorKind`.
        title: Short headline ("Bedrock model access not granted").
        hint: Multi-line action steps the user can copy verbatim.
        raw_message: The original exception string for debug logs.
        aws_error_code: AWS error code (``AccessDeniedException`` etc.)
            when extractable, else ``None``.
    """

    kind: BedrockErrorKind
    title: str
    hint: str
    raw_message: str
    aws_error_code: str | None = None

    def banner(self) -> str:
        """Format as a high-visibility multi-line block for stderr/chat.

        Designed to stand out in a wall of LangGraph noise. Width is
        capped to 78 chars so it fits a typical terminal without wrap.
        """
        bar = "=" * 78
        title = f"BEDROCK: {self.title}"
        # Truncate the hint into 78-char lines.
        hint_lines = []
        for source_line in self.hint.splitlines():
            if len(source_line) <= 78:
                hint_lines.append(source_line)
                continue
            # Naive wrap at a word boundary near 78.
            remaining = source_line
            while len(remaining) > 78:
                cut = remaining.rfind(" ", 0, 78)
                if cut == -1:
                    cut = 78
                hint_lines.append(remaining[:cut])
                remaining = remaining[cut:].lstrip()
            if remaining:
                hint_lines.append(remaining)
        return "\n".join(
            [
                "",
                bar,
                title,
                bar,
                *hint_lines,
                bar,
                f"[debug] kind={self.kind.value}"
                + (f"  aws_code={self.aws_error_code}" if self.aws_error_code else ""),
                f"[debug] raw: {self.raw_message[:300]}",
                bar,
                "",
            ]
        )


def _extract_aws_error_code(exc: BaseException) -> str | None:
    """Pull AWS error code out of a boto ``ClientError`` if present."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error")
        if isinstance(err, dict):
            code = err.get("Code")
            if isinstance(code, str):
                return code
    return None


_CREDENTIAL_MISSING_PATTERNS = (
    "could not find credentials",
    "unable to locate credentials",
    "no credentials in the property bag",
    "no credentials provided",
)

_CREDENTIAL_EXPIRED_PATTERNS = (
    "expired",
    "tokenexpired",
    "ssotokenloaderror",
    "ssoauthorizationexpired",
)

_MODEL_ACCESS_PATTERNS = (
    "you don't have access to the model",
    "access denied",
    "is not authorized to perform: bedrock",
)

_REGION_PATTERNS = (
    "could not find aws_default_region",
    "you must specify a region",
    "no_region_error",
    "endpoint resolved to",
    "endpointconnectionerror",
)

_MODEL_ID_PATTERNS = (
    "validationexception",
    "invalid model identifier",
    "the provided model identifier is invalid",
    "the requested model id is not supported",
)

_NETWORK_PATTERNS = (
    "could not connect to the endpoint",
    "name resolution",
    "ssl",
    "connection",
)


def categorize_bedrock_error(exc: BaseException) -> BedrockError:
    """Map a Bedrock-related exception to a :class:`BedrockError`.

    Order matters: the most specific categories (model access, region)
    are checked before the catch-all ``CREDENTIALS_MISSING`` case so a
    "no credentials" message that *also* happens to mention a region
    doesn't get mis-classified.
    """
    code = _extract_aws_error_code(exc) or ""
    msg_lower = str(exc).lower()
    raw = str(exc)

    # Package missing — caller should detect this BEFORE calling us, but
    # handle it gracefully if the import error leaks through. This covers
    # both the raw ``ModuleNotFoundError`` and the SDK's wrapped
    # ``ModelConfigError("Missing package for provider 'bedrock'. Install:
    # pip install langchain-aws")`` so the user gets the CLI-specific extra
    # hint instead of an UNKNOWN catch-all.
    pkg_named = (
        "langchain_aws" in msg_lower
        or "langchain-aws" in msg_lower
        or "boto3" in msg_lower
        or "botocore" in msg_lower
    )
    if pkg_named and (
        "no module named" in msg_lower or "missing package for provider" in msg_lower
    ):
        return BedrockError(
            kind=BedrockErrorKind.PACKAGE_MISSING,
            title="langchain-aws / boto3 not installed",
            hint=(
                "Install the AWS extra:\n"
                "  pip install --upgrade 'bog-agents-cli[bedrock]'\n"
                "or:\n"
                "  pip install --upgrade langchain-aws boto3"
            ),
            raw_message=raw,
            aws_error_code=None,
        )

    # ThrottlingException / RateLimit — happens during sustained traffic.
    if (
        code in ("ThrottlingException", "TooManyRequestsException")
        or "throttl" in msg_lower
    ):
        return BedrockError(
            kind=BedrockErrorKind.THROTTLED,
            title="Bedrock throttled the request",
            hint=(
                "AWS Bedrock has per-account request quotas. Options:\n"
                "  - Wait a few seconds and retry (provider_retry middleware "
                "does this automatically with exponential backoff).\n"
                "  - Request a quota increase in the AWS console under "
                "Service Quotas → Amazon Bedrock.\n"
                "  - Spread load across multiple model IDs (use "
                "`/model` to switch)."
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    # AccessDeniedException — most common Bedrock first-time-user error.
    if code == "AccessDeniedException" or any(
        p in msg_lower for p in _MODEL_ACCESS_PATTERNS
    ):
        return BedrockError(
            kind=BedrockErrorKind.MODEL_ACCESS_DENIED,
            title="Bedrock model access NOT granted",
            hint=(
                "AWS requires you to request access to each Bedrock model "
                "before invoking it.\n"
                "Steps:\n"
                "  1. Open https://console.aws.amazon.com/bedrock/\n"
                "  2. Switch to your target region (top-right).\n"
                "  3. Left sidebar → 'Model access'.\n"
                "  4. Click 'Modify model access', tick the model(s) you "
                "want, submit.\n"
                "  5. Wait 1-2 minutes for the grant to propagate.\n"
                "If you've already granted access, double-check the "
                "AWS_REGION env var matches the region where you granted "
                "it — model access is per-region."
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    # Validation on the model id itself.
    if code == "ValidationException" or any(p in msg_lower for p in _MODEL_ID_PATTERNS):
        return BedrockError(
            kind=BedrockErrorKind.MODEL_ID_INVALID,
            title="Bedrock did not accept the model id",
            hint=(
                "The model id was rejected. Common causes:\n"
                "  - Wrong format. For converse-API models use the "
                "`bedrock_converse:<id>` prefix in bog-agents.\n"
                "  - Wrong region — some models are region-restricted.\n"
                "  - Profile-based model id (e.g. "
                "`us.anthropic.claude-...`) used in a non-cross-region "
                "setup.\n"
                "Run `bog-agents test-bedrock` to see which model IDs "
                "your account can actually invoke."
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    # Region/endpoint issues.
    if any(p in msg_lower for p in _REGION_PATTERNS):
        return BedrockError(
            kind=BedrockErrorKind.REGION_INVALID,
            title="AWS region misconfigured or unreachable",
            hint=(
                "Bedrock requires a region. Set one of:\n"
                "  export AWS_REGION=us-east-1\n"
                "  export AWS_DEFAULT_REGION=us-east-1\n"
                "or in ~/.aws/config under your active profile.\n"
                "If the region is set but Bedrock still can't reach the "
                "endpoint, you may be on a network that blocks "
                "bedrock-runtime.<region>.amazonaws.com — check "
                "corporate VPN / firewall rules."
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    # Credential expiry comes BEFORE the missing-credentials check
    # because the patterns overlap (both can mention 'credentials').
    if any(p in msg_lower for p in _CREDENTIAL_EXPIRED_PATTERNS):
        return BedrockError(
            kind=BedrockErrorKind.CREDENTIALS_EXPIRED,
            title="AWS credentials expired",
            hint=(
                "Your AWS session expired. Options:\n"
                "  - SSO: run `aws sso login` and try again.\n"
                "  - STS: re-export AWS_SESSION_TOKEN with a fresh value.\n"
                "  - Long-lived keys: shouldn't expire — re-check "
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are still "
                "active in the IAM console."
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    if any(p in msg_lower for p in _CREDENTIAL_MISSING_PATTERNS):
        return BedrockError(
            kind=BedrockErrorKind.CREDENTIALS_MISSING,
            title="No AWS credentials found",
            hint=(
                "boto3 couldn't find any credentials. Set up one:\n"
                "  - `aws configure` to write ~/.aws/credentials\n"
                "  - `aws sso login` for AWS SSO\n"
                "  - Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars\n"
                "  - Set AWS_PROFILE to point at a profile in "
                "~/.aws/config\n"
                "Verify with: `aws sts get-caller-identity`"
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    # Network/DNS — generic connectivity. Check after auth/region so
    # auth issues that mention "connection" don't get mis-categorised.
    if any(p in msg_lower for p in _NETWORK_PATTERNS):
        return BedrockError(
            kind=BedrockErrorKind.NETWORK,
            title="Network or TLS error reaching Bedrock",
            hint=(
                "Could not reach the Bedrock endpoint. Check:\n"
                "  - Internet connectivity (`curl -v "
                "https://bedrock-runtime.us-east-1.amazonaws.com`)\n"
                "  - Corporate proxy / VPN — set HTTPS_PROXY if needed.\n"
                "  - System time — TLS rejects requests with skewed "
                "clocks. Run `w32tm /resync` (Windows) or `chronyc "
                "makestep` (Linux)."
            ),
            raw_message=raw,
            aws_error_code=code or None,
        )

    # Last resort.
    return BedrockError(
        kind=BedrockErrorKind.UNKNOWN,
        title="Unrecognised Bedrock error",
        hint=(
            f"The error didn't match any known category. Raw message:\n"
            f"  {raw[:500]}\n"
            "Run `bog-agents test-bedrock` for a structured probe of "
            "credentials, region, and inference."
        ),
        raw_message=raw,
        aws_error_code=code or None,
    )


# ---------------------------------------------------------------------------
# Connection probe — used by both ``test-bedrock`` and ``/bedrock test``
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeStep:
    """One step of the connection probe.

    Attributes:
        name: Short display name (``"Credentials"``, ``"Region"`` etc).
        ok: Whether the step succeeded.
        detail: One-line summary for the success path.
        error: ``BedrockError`` for the failure path.
    """

    name: str
    ok: bool
    detail: str
    error: BedrockError | None = None


def probe_bedrock(
    model_id: str | None = None,
    region: str | None = None,
) -> list[ProbeStep]:
    """Run the multi-step Bedrock connection probe.

    Steps (each independent — later steps run even when earlier ones
    fail, so the user sees the full picture):

    1. Package: is ``langchain-aws`` / ``boto3`` importable?
    2. Credentials: does boto3 find any?
    3. Region: is one set?
    4. Caller identity: ``sts get-caller-identity`` works?
    5. Model access: list bedrock foundation models in region.
    6. Inference: a tiny ``converse`` call against ``model_id``.

    Args:
        model_id: The Bedrock model id to test inference against.
            When ``None``, inference is skipped (steps 1-5 still run).
        region: Region override. When ``None``, boto3's default chain
            (env vars → ``~/.aws/config``) is used.

    Returns:
        List of :class:`ProbeStep` in the order they ran.
    """
    steps: list[ProbeStep] = []

    # --- step 1: package import ---
    try:
        import boto3
        import botocore  # noqa: F401
    except ImportError as exc:
        steps.append(
            ProbeStep(
                name="Package",
                ok=False,
                detail="langchain-aws / boto3 not installed",
                error=categorize_bedrock_error(exc),
            )
        )
        # Bail — without boto3 nothing else can run.
        return steps
    steps.append(ProbeStep(name="Package", ok=True, detail="boto3 available"))

    # --- step 2: credentials ---
    try:
        session = boto3.Session(region_name=region) if region else boto3.Session()
        creds = session.get_credentials()
        if creds is None:
            err = ImportError("Could not locate credentials")  # placeholder to route
            err.args = ("Could not locate credentials",)
            cat = BedrockError(
                kind=BedrockErrorKind.CREDENTIALS_MISSING,
                title="No AWS credentials found",
                hint=(
                    "Set up one of:\n"
                    "  aws configure\n"
                    "  aws sso login\n"
                    "  AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars\n"
                    "Verify: aws sts get-caller-identity"
                ),
                raw_message="boto3.Session().get_credentials() returned None",
            )
            steps.append(
                ProbeStep(
                    name="Credentials",
                    ok=False,
                    detail="No credentials in boto3 chain",
                    error=cat,
                )
            )
        else:
            method = getattr(creds, "method", "unknown")
            steps.append(
                ProbeStep(
                    name="Credentials",
                    ok=True,
                    detail=f"Found via {method}",
                )
            )
    except Exception as exc:  # diagnostic: never re-raise
        steps.append(
            ProbeStep(
                name="Credentials",
                ok=False,
                detail="boto3.Session() raised",
                error=categorize_bedrock_error(exc),
            )
        )

    # --- step 3: region ---
    resolved_region = (
        region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not resolved_region:
        try:
            sess = boto3.Session()
            resolved_region = sess.region_name
        except Exception:  # diagnostic only
            resolved_region = None
    if resolved_region:
        steps.append(
            ProbeStep(
                name="Region",
                ok=True,
                detail=resolved_region,
            )
        )
    else:
        steps.append(
            ProbeStep(
                name="Region",
                ok=False,
                detail="No AWS_REGION / AWS_DEFAULT_REGION / profile region",
                error=BedrockError(
                    kind=BedrockErrorKind.REGION_INVALID,
                    title="No AWS region configured",
                    hint=(
                        "Set AWS_REGION=us-east-1 (or your target region) "
                        "in the env or via `aws configure`."
                    ),
                    raw_message="resolved region was None",
                ),
            )
        )

    # --- step 4: caller identity ---
    try:
        sts = boto3.client("sts", region_name=resolved_region)
        ident = sts.get_caller_identity()
        steps.append(
            ProbeStep(
                name="Identity",
                ok=True,
                detail=f"{ident.get('Arn', '<no arn>')}",
            )
        )
    except Exception as exc:  # diagnostic only
        steps.append(
            ProbeStep(
                name="Identity",
                ok=False,
                detail="sts.get_caller_identity() failed",
                error=categorize_bedrock_error(exc),
            )
        )

    # --- step 5: list foundation models ---
    try:
        bedrock = boto3.client("bedrock", region_name=resolved_region)
        models_resp = bedrock.list_foundation_models()
        # Surface a small sample so the user can see model IDs to use.
        summaries = models_resp.get("modelSummaries", [])
        sample = ", ".join(
            m.get("modelId", "?") for m in summaries[:3] if m.get("modelId")
        )
        more = max(0, len(summaries) - 3)
        suffix = f" (+{more} more)" if more else ""
        steps.append(
            ProbeStep(
                name="ListModels",
                ok=True,
                detail=f"{len(summaries)} models in region: {sample}{suffix}",
            )
        )
    except Exception as exc:  # diagnostic only
        steps.append(
            ProbeStep(
                name="ListModels",
                ok=False,
                detail="bedrock.list_foundation_models() failed",
                error=categorize_bedrock_error(exc),
            )
        )

    # --- step 6: tiny inference ---
    if model_id:
        try:
            client = boto3.client("bedrock-runtime", region_name=resolved_region)
            response = client.converse(
                modelId=model_id,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": "Say PROBE_OK and nothing else."}],
                    }
                ],
                inferenceConfig={"maxTokens": 10, "temperature": 0.0},
            )
            usage = response.get("usage", {})
            tokens = usage.get("totalTokens") or usage.get(
                "inputTokens", 0
            ) + usage.get("outputTokens", 0)
            steps.append(
                ProbeStep(
                    name="Inference",
                    ok=True,
                    detail=f"converse() OK ({tokens} tokens)",
                )
            )
        except Exception as exc:  # diagnostic only
            steps.append(
                ProbeStep(
                    name="Inference",
                    ok=False,
                    detail=f"converse() failed for {model_id}",
                    error=categorize_bedrock_error(exc),
                )
            )

    return steps


def render_probe_report(steps: list[ProbeStep]) -> str:
    """Render a probe result list as a human-readable report.

    Used by both the CLI subcommand and the ``/bedrock test`` slash
    command so output stays consistent.
    """

    def _icon(ok: bool) -> str:
        return "[OK]  " if ok else "[FAIL]"

    lines: list[str] = ["", "Bedrock connection probe", "=" * 40]
    for step in steps:
        lines.append(f"{_icon(step.ok)}  {step.name:<13}  {step.detail}")
    failures = [s for s in steps if not s.ok]
    if failures:
        lines.append("")
        lines.append("Failure details")
        lines.append("-" * 40)
        for step in failures:
            err = step.error
            if err is not None:
                lines.append(err.banner())
    else:
        lines.append("")
        lines.append("All checks passed.")
    return "\n".join(lines)


def iter_probe_lines(steps: list[ProbeStep]) -> Iterator[str]:
    """Yield report lines one at a time — useful for streaming UIs."""
    yield from render_probe_report(steps).splitlines()


# ---------------------------------------------------------------------------
# /bedrock fix — turn each probe failure into a copy-paste command
# ---------------------------------------------------------------------------

# Maps each BedrockErrorKind to (one-line headline, command(s) the user
# can copy-paste). The commands are intentionally minimal — the goal is
# "press Ctrl+C, run this, try again" rather than a 6-step recipe. The
# bedrock URLs and CLI commands are stable enough that hardcoding them
# is fine; if AWS reorganises a console URL we update one constant.
_FIX_ACTIONS: dict[BedrockErrorKind, tuple[str, tuple[str, ...]]] = {
    BedrockErrorKind.PACKAGE_MISSING: (
        "Install the AWS extra.",
        ("pip install --upgrade 'bog-agents-cli[bedrock]'",),
    ),
    BedrockErrorKind.CREDENTIALS_MISSING: (
        "Set up AWS credentials (pick the one that matches your account).",
        (
            "aws configure                  # long-lived keys",
            "aws sso login                  # AWS SSO",
            "export AWS_ACCESS_KEY_ID=...   # one-shot env vars",
            "export AWS_SECRET_ACCESS_KEY=...",
        ),
    ),
    BedrockErrorKind.CREDENTIALS_EXPIRED: (
        "Refresh your AWS session.",
        (
            "aws sso login                  # if SSO",
            "# or re-export AWS_SESSION_TOKEN with a fresh value",
        ),
    ),
    BedrockErrorKind.REGION_INVALID: (
        "Set an AWS region (Bedrock requires one).",
        (
            "export AWS_REGION=us-east-1    # or your target region",
            "# or: aws configure set region us-east-1",
        ),
    ),
    BedrockErrorKind.MODEL_ACCESS_DENIED: (
        "Request model access in the Bedrock console (per region).",
        (
            "# 1. open https://console.aws.amazon.com/bedrock/",
            "# 2. switch to your target region (top-right)",
            "# 3. left sidebar → 'Model access' → 'Modify model access'",
            "# 4. tick the model(s), submit, wait ~1 minute",
        ),
    ),
    BedrockErrorKind.MODEL_ID_INVALID: (
        "The model id was rejected — try the cross-region inference profile id.",
        (
            "# Claude 4.x on Bedrock requires a prefix: us. / eu. / apac.",
            "/model bedrock_converse:us.anthropic.claude-opus-4-8",
            "# or list what your account can invoke:",
            "bog-agents test-bedrock",
        ),
    ),
    BedrockErrorKind.THROTTLED: (
        "Bedrock is throttling — retry, switch model, or request a quota bump.",
        (
            "# Service Quotas → Amazon Bedrock → request increase",
            "# https://console.aws.amazon.com/servicequotas/home/services/bedrock/",
        ),
    ),
    BedrockErrorKind.NETWORK: (
        "Network or TLS error — check connectivity and system clock.",
        (
            "curl -v https://bedrock-runtime.us-east-1.amazonaws.com",
            "# if behind a corp proxy: export HTTPS_PROXY=http://proxy.corp:8080",
        ),
    ),
    BedrockErrorKind.UNKNOWN: (
        "Unrecognised error — capture the raw message and open an issue.",
        ("bog-agents test-bedrock        # structured probe with verbose output",),
    ),
}


def render_settings_report() -> str:
    """Render a human-readable view of the active Bedrock configuration.

    Surfaces the effective auth mode, AWS profile, region, and the
    config file path so a user can see at a glance what's wired up
    before they call `/bedrock test`. Renders an example ``config.toml``
    block at the bottom so a user can copy-paste the minimum settings
    they need to change.

    Returns:
        Multi-line report.
    """
    # Lazy imports to keep _bedrock importable in envs where the heavier
    # model_config module isn't ready (e.g. unit tests for fix_report).
    try:
        from bog_agents_cli.model_config import (
            DEFAULT_CONFIG_PATH,
            _bedrock_auth_mode,
        )
    except ImportError:
        return "Bedrock settings unavailable (model_config import failed)."

    try:
        mode, profile = _bedrock_auth_mode()
    except Exception as exc:  # diagnostic only
        return f"Bedrock settings: error reading config — {exc}"

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "<unset>"
    )
    aws_profile_env = os.environ.get("AWS_PROFILE", "<unset>")
    config_path = DEFAULT_CONFIG_PATH

    lines = [
        "",
        "Bedrock settings",
        "=" * 40,
        f"  auth_mode      : {mode}",
        f"  aws_profile    : {profile or '<unset>'}",
        f"  AWS_PROFILE env: {aws_profile_env}",
        f"  region         : {region}",
        f"  config file    : {config_path}",
        "",
        "To change, edit the config file above and add:",
        "",
        "  [models.providers.bedrock_converse]",
        '  auth_mode   = "auto"        # auto | iam | profile | sso | env',
        '  aws_profile = "my-profile"  # only when auth_mode = profile',
        "",
        "Or set the equivalent env vars:",
        "",
        "  export BOG_AGENTS_BEDROCK_AUTH_MODE=sso",
        "  export BOG_AGENTS_BEDROCK_PROFILE=my-profile",
        "  export AWS_REGION=us-east-1",
        "",
    ]
    return "\n".join(lines)


def render_fix_report(steps: list[ProbeStep]) -> str:
    """Render probe failures as a list of copy-paste actions.

    Sister to :func:`render_probe_report`, but optimised for the "I'm
    stuck, what do I run next?" path. Successful steps are summarised in
    one line; each failure gets a numbered block with a headline and
    one or more shell commands the user can copy verbatim.

    Args:
        steps: Probe steps from :func:`probe_bedrock`.

    Returns:
        Multi-line report with shell-ready commands. When no failures
        occurred, returns a short "All clear" block instead.
    """
    failures = [s for s in steps if not s.ok and s.error is not None]
    if not failures:
        return (
            "\n"
            "Bedrock /bedrock fix\n"
            "=" * 40 + "\n"
            "All probe steps passed — no action needed.\n"
            f"Steps OK: {', '.join(s.name for s in steps if s.ok)}\n"
        )

    lines: list[str] = ["", "Bedrock /bedrock fix", "=" * 40, ""]
    # Brief summary line up top so the user knows the scope.
    ok_names = [s.name for s in steps if s.ok]
    if ok_names:
        lines.append(f"OK: {', '.join(ok_names)}")
    lines.append(f"Failed: {', '.join(s.name for s in failures)}")
    lines.append("")
    for idx, step in enumerate(failures, start=1):
        err = step.error
        if err is None:  # defensive — already filtered above
            continue
        headline, commands = _FIX_ACTIONS.get(
            err.kind,
            _FIX_ACTIONS[BedrockErrorKind.UNKNOWN],
        )
        lines.append(f"[{idx}] {step.name} — {err.title}")
        lines.append(f"    {headline}")
        for cmd in commands:
            lines.append(f"      {cmd}")
        lines.append("")
    lines.append("After the fix, re-run /bedrock test (or press Ctrl+T in /model).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probe-and-pick — choose a model the account can actually invoke
# ---------------------------------------------------------------------------


def pick_hittable_bedrock_model(
    candidates: Sequence[str],
    region: str | None = None,
    *,
    max_attempts: int = 5,
) -> tuple[str | None, BedrockError | None]:
    """Return the first candidate Bedrock model id the account can invoke.

    Each candidate is tested with a tiny (1-token) ``converse`` call in
    preference order; the first that succeeds is returned. This is what lets
    a fresh user "default to a model we KNOW is hittable" instead of being
    pinned to the most access-gated model and seeing an opaque error on their
    first prompt.

    Account-wide failures (missing/expired credentials, no region, network,
    package missing) abort the probe early — no candidate could overcome them
    — and the categorized error is returned so the caller can show the real
    next step. Per-model failures (access not granted, invalid id, throttled,
    unknown) skip to the next candidate.

    Args:
        candidates: Ordered Bedrock model ids, most preferred first (e.g.
            ``("us.anthropic.claude-opus-4-8", "us.amazon.nova-lite-v1:0")``).
        region: AWS region for the probe. When ``None``, falls back to
            ``AWS_REGION`` / ``AWS_DEFAULT_REGION``.
        max_attempts: Cap on how many candidates to probe (bounds latency and
            cost on a fully locked-down account).

    Returns:
        ``(model_id, None)`` for the first invokable candidate, or
        ``(None, BedrockError)`` when none are reachable. Account-wide errors
        take precedence over the first per-model failure in the returned
        error.
    """
    try:
        import boto3
    except ImportError as exc:
        return None, categorize_bedrock_error(exc)

    resolved = (
        region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    )
    try:
        client = boto3.client("bedrock-runtime", region_name=resolved)
    except Exception as exc:  # diagnostic — never raise out of the probe
        return None, categorize_bedrock_error(exc)

    account_wide = {
        BedrockErrorKind.CREDENTIALS_MISSING,
        BedrockErrorKind.CREDENTIALS_EXPIRED,
        BedrockErrorKind.REGION_INVALID,
        BedrockErrorKind.NETWORK,
        BedrockErrorKind.PACKAGE_MISSING,
    }
    first_error: BedrockError | None = None
    for model_id in list(candidates)[:max_attempts]:
        try:
            client.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                inferenceConfig={"maxTokens": 1, "temperature": 0.0},
            )
        except Exception as exc:
            err = categorize_bedrock_error(exc)
            if first_error is None:
                first_error = err
            if err.kind in account_wide:
                return None, err
            continue
        else:
            return model_id, None
    return None, first_error


__all__ = [
    "BedrockError",
    "BedrockErrorKind",
    "ProbeStep",
    "categorize_bedrock_error",
    "iter_probe_lines",
    "pick_hittable_bedrock_model",
    "probe_bedrock",
    "render_fix_report",
    "render_probe_report",
    "render_settings_report",
]
