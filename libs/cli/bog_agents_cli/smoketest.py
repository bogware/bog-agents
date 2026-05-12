"""Smoke-test a model+provider combo end-to-end.

Verifies credentials, network reachability, and that the configured model
can complete a tiny one-token request. Used by the `/smoketest` slash
command and the model picker's Ctrl+T binding.

Design goals
------------
* **Cheap**: sends one short prompt with a 1-token output budget. For
  Anthropic/OpenAI this costs well under a tenth of a cent per call.
* **Actionable errors**: maps the most common failure shapes
  (missing/expired credentials, network unreachable, throttling,
  model-id-not-found) to a short hint the user can paste into a shell.
* **Bedrock-aware**: delegates to the existing multi-step ``probe_bedrock``
  reporter so the user sees the same probe report as ``/bedrock test``.
* **No retries / no fallback**: a smoketest is a diagnostic — if it
  fails, the user wants to see *the* failure, not a retried success.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Tight upper bound — smoketests should never burn more than a few cents.
_SMOKETEST_TIMEOUT_SECONDS = 30.0

# The prompt is intentionally trivial so we can demand a single-token
# reply. Some providers refuse zero-token outputs, so we ask for "OK".
_SMOKETEST_PROMPT = (
    "Reply with exactly the single uppercase word: OK. "
    "Do not add punctuation or explanation."
)


class SmoketestKind(StrEnum):
    """High-level outcome categories the user can act on."""

    OK = "ok"
    """Provider responded with a non-empty body."""

    AUTH_MISSING = "auth_missing"
    """No credentials configured (env var unset, no AWS config, etc.)."""

    AUTH_INVALID = "auth_invalid"
    """Credentials present but rejected (401, expired token, bad key)."""

    NETWORK = "network"
    """DNS / connection / TLS — could not reach the API."""

    MODEL_NOT_FOUND = "model_not_found"
    """Provider rejected the model id (typo, region mismatch, retired)."""

    QUOTA = "quota"
    """Rate limit / quota exceeded — retry after backoff."""

    TIMEOUT = "timeout"
    """Call did not complete within the smoketest budget."""

    PACKAGE_MISSING = "package_missing"
    """Provider SDK not installed (e.g. ``langchain-aws``)."""

    UNKNOWN = "unknown"
    """Failure could not be categorised; raw error preserved."""


@dataclass(frozen=True)
class SmoketestResult:
    """Structured outcome of a model smoketest.

    Attributes:
        spec: The ``provider:model`` string that was tested.
        kind: One of :class:`SmoketestKind`.
        elapsed_seconds: Wall-clock time for the test call.
        message: One-line human-readable summary.
        hint: Multi-line follow-up steps when ``kind`` is not ``OK``.
        thinking_used: Whether the test exercised extended-thinking
            parameters (only meaningful when ``kind`` is ``OK``).
        raw_error: The underlying exception string (for logs).
    """

    spec: str
    kind: SmoketestKind
    elapsed_seconds: float
    message: str
    hint: str = ""
    thinking_used: bool = False
    raw_error: str = ""

    @property
    def ok(self) -> bool:
        """True when the model responded successfully."""
        return self.kind == SmoketestKind.OK

    def summary_markup(self) -> str:
        """Render a one-line Rich-markup status for the picker footer."""
        if self.ok:
            tag = "[green]PASS[/green]"
            extra = " [magenta]thinking ✓[/magenta]" if self.thinking_used else ""
            return f"{tag} {self.spec} ({self.elapsed_seconds:.1f}s){extra}"
        return f"[red]FAIL[/red] {self.spec}: {self.message}"

    def report_text(self) -> str:
        """Render a multi-line report block for the chat surface."""
        lines = [
            f"Smoketest: {self.spec}",
            f"  kind:    {self.kind.value}",
            f"  elapsed: {self.elapsed_seconds:.2f}s",
            f"  status:  {self.message}",
        ]
        if self.thinking_used:
            lines.append("  thinking: enabled (provider accepted budget_tokens)")
        if self.hint:
            lines.append("")
            lines.extend(f"  hint: {line}" for line in self.hint.splitlines())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Error categorization
# ---------------------------------------------------------------------------


_AUTH_INVALID_SIGNALS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "invalid_api_key",
    "invalid api key",
    "authentication",
    "expiredtoken",
    "tokenretrieval",
    "credentials",
)
_NETWORK_SIGNALS = (
    "connection refused",
    "connection error",
    "name or service not known",
    "name resolution",
    "temporary failure in name resolution",
    "no address associated with hostname",
    "dns",
    "ssl",
    "tls",
    "timed out",
    "timeout",
    "network is unreachable",
    "could not connect",
    "ehostunreach",
    "econnreset",
    "endpoint connection error",
)
_NOT_FOUND_SIGNALS = (
    "model not found",
    "model_not_found",
    "modelid",
    "validationexception",
    "does not exist",
    "unknown model",
    "no such model",
    "not authorized to invoke",
)
_QUOTA_SIGNALS = (
    "rate limit",
    "rate_limit",
    "throttl",
    "too many requests",
    "429",
    "quota",
    "service unavailable",
    "503",
)
_AUTH_MISSING_SIGNALS = (
    "no api key",
    "missing api key",
    "set the.*api.*environment variable",
    "could not load credentials",
    "unable to locate credentials",
)


def _categorize(exc: BaseException) -> tuple[SmoketestKind, str]:
    """Return ``(kind, short_summary)`` for an arbitrary exception."""
    text = str(exc).lower()
    name = type(exc).__name__

    if name in ("TimeoutError", "ReadTimeout", "ConnectTimeout"):
        return SmoketestKind.TIMEOUT, "request timed out"

    if name == "ImportError" or "no module named" in text:
        return SmoketestKind.PACKAGE_MISSING, str(exc)

    for signal in _AUTH_MISSING_SIGNALS:
        if signal in text:
            return SmoketestKind.AUTH_MISSING, "credentials not configured"
    for signal in _AUTH_INVALID_SIGNALS:
        if signal in text:
            return SmoketestKind.AUTH_INVALID, "credentials rejected"
    for signal in _NOT_FOUND_SIGNALS:
        if signal in text:
            return SmoketestKind.MODEL_NOT_FOUND, "model id rejected by provider"
    for signal in _QUOTA_SIGNALS:
        if signal in text:
            return SmoketestKind.QUOTA, "quota / throttling — retry after backoff"
    for signal in _NETWORK_SIGNALS:
        if signal in text:
            return SmoketestKind.NETWORK, "could not reach provider endpoint"

    return SmoketestKind.UNKNOWN, str(exc).splitlines()[0][:160] if str(exc) else name


def _hint_for(kind: SmoketestKind, provider: str) -> str:
    """Render an actionable next-step hint for a failure category."""
    if kind == SmoketestKind.AUTH_MISSING:
        if provider in ("bedrock", "bedrock_converse"):
            return (
                "AWS credentials not detected.\n"
                "Run `aws configure` or `aws sso login`, "
                "or set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY."
            )
        env_hint = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "google_genai": "GOOGLE_API_KEY",
            "groq": "GROQ_API_KEY",
            "mistralai": "MISTRAL_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "xai": "XAI_API_KEY",
        }.get(provider)
        if env_hint:
            return f"Set {env_hint} in your environment (or via /vars)."
        return "Configure the provider's credentials in your environment."
    if kind == SmoketestKind.AUTH_INVALID:
        if provider in ("bedrock", "bedrock_converse"):
            return (
                "AWS credentials are present but rejected.\n"
                "If using SSO, run `aws sso login`. If using static keys, "
                "verify they're still active in the AWS console."
            )
        return "Check that the configured API key is still valid and has access."
    if kind == SmoketestKind.MODEL_NOT_FOUND:
        if provider in ("bedrock", "bedrock_converse"):
            return (
                "Bedrock rejected the modelId. Possible causes:\n"
                "• You haven't requested model access in the Bedrock console.\n"
                "• The model isn't available in your configured region.\n"
                "• You used a base id where an inference profile id is required "
                "(prefer `us.anthropic.…` / `eu.anthropic.…` for newer models)."
            )
        return "Verify the model id is current — providers retire ids frequently."
    if kind == SmoketestKind.NETWORK:
        return (
            "Could not reach the provider endpoint.\n"
            "Check internet connectivity, corporate proxy, and firewall rules."
        )
    if kind == SmoketestKind.QUOTA:
        return "Wait and retry, or request a quota increase from the provider."
    if kind == SmoketestKind.TIMEOUT:
        return (
            f"Call exceeded {_SMOKETEST_TIMEOUT_SECONDS:.0f}s. "
            "The provider may be slow or your network may be flaky."
        )
    if kind == SmoketestKind.PACKAGE_MISSING:
        return (
            "Install the provider's package, e.g. "
            "`uv pip install langchain-aws` for Bedrock."
        )
    return ""


# ---------------------------------------------------------------------------
# Bedrock smoketest (delegates to the existing probe)
# ---------------------------------------------------------------------------


def _smoketest_bedrock(spec: str, model_name: str) -> SmoketestResult:
    """Run the Bedrock probe and translate its result to a SmoketestResult."""
    start = time.monotonic()
    try:
        from bog_agents_cli._bedrock import probe_bedrock
    except ImportError as exc:
        return SmoketestResult(
            spec=spec,
            kind=SmoketestKind.PACKAGE_MISSING,
            elapsed_seconds=time.monotonic() - start,
            message="bedrock probe module unavailable",
            hint=_hint_for(SmoketestKind.PACKAGE_MISSING, "bedrock"),
            raw_error=str(exc),
        )

    steps = probe_bedrock(model_name, None)
    elapsed = time.monotonic() - start

    failed_step = next((s for s in steps if not s.ok), None)
    if failed_step is None:
        return SmoketestResult(
            spec=spec,
            kind=SmoketestKind.OK,
            elapsed_seconds=elapsed,
            message="all probe steps passed",
        )

    bedrock_err = getattr(failed_step, "error", None)
    raw = str(bedrock_err) if bedrock_err else failed_step.detail or failed_step.name
    kind, summary = _categorize(Exception(raw or failed_step.name))
    return SmoketestResult(
        spec=spec,
        kind=kind,
        elapsed_seconds=elapsed,
        message=f"failed at step '{failed_step.name}': {summary}",
        hint=_hint_for(kind, "bedrock_converse"),
        raw_error=raw,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def smoketest_model(
    spec: str,
    *,
    thinking: bool = False,
    timeout_seconds: float = _SMOKETEST_TIMEOUT_SECONDS,
) -> SmoketestResult:
    """Smoke-test a `provider:model` combo.

    Args:
        spec: ``provider:model`` string (e.g. ``"anthropic:claude-sonnet-4-6"``).
            Bare model names are accepted but discouraged — the result
            ``spec`` will reflect the resolved provider.
        thinking: When ``True`` and the model supports extended thinking,
            ask the provider to enable thinking with a small budget.
            The test still expects a one-word reply; we only verify that
            the provider accepts the thinking parameter without 400ing.
        timeout_seconds: Hard cap on the inference call.

    Returns:
        A structured result. Always returns; never raises.
    """
    # Parse the spec to route Bedrock through its dedicated probe.
    provider, _, model_name = spec.partition(":")
    if not provider or not model_name:
        # Bare model name → defer to create_model's auto-detection.
        from bog_agents_cli.config import detect_provider

        detected = detect_provider(spec)
        if detected:
            provider, model_name = detected, spec
            spec = f"{provider}:{model_name}"
        else:
            return SmoketestResult(
                spec=spec,
                kind=SmoketestKind.UNKNOWN,
                elapsed_seconds=0.0,
                message="could not determine provider — use provider:model format",
            )

    if provider in ("bedrock", "bedrock_converse"):
        return _smoketest_bedrock(spec, model_name)

    # Generic path: create model + send tiny ainvoke.
    start = time.monotonic()
    try:
        from bog_agents_cli.config import create_model

        extra_kwargs: dict[str, Any] = {"max_tokens": 16}
        # Try to bind thinking when requested AND the model supports it.
        if thinking:
            from bog_agents_cli.provider_catalog import supports_native_thinking

            if supports_native_thinking(model_name):
                # Provider-specific binding handled by ChatAnthropic etc.
                # Use a small budget — we don't need real reasoning, just
                # confirm the parameter is accepted.
                if provider == "anthropic":
                    extra_kwargs["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": 1024,
                    }
                elif provider in ("openai", "azure_openai"):
                    extra_kwargs["reasoning_effort"] = "low"

        result = create_model(spec, extra_kwargs=extra_kwargs)
        model = result.model
    except Exception as exc:
        elapsed = time.monotonic() - start
        kind, summary = _categorize(exc)
        return SmoketestResult(
            spec=spec,
            kind=kind,
            elapsed_seconds=elapsed,
            message=f"model construction failed: {summary}",
            hint=_hint_for(kind, provider),
            raw_error=str(exc),
        )

    # Invoke synchronously — boto3 / requests-based providers are sync;
    # async wrappers add a layer of event-loop binding pain we don't need
    # for a one-shot smoketest. Threaded callers can wrap in to_thread.
    try:
        import threading

        response_holder: list[Any] = []
        error_holder: list[BaseException] = []

        def _call() -> None:
            try:
                response_holder.append(model.invoke(_SMOKETEST_PROMPT))
            except BaseException as exc:
                error_holder.append(exc)

        thread = threading.Thread(target=_call, daemon=True, name="smoketest")
        thread.start()
        thread.join(timeout=timeout_seconds)
        if thread.is_alive():
            elapsed = time.monotonic() - start
            return SmoketestResult(
                spec=spec,
                kind=SmoketestKind.TIMEOUT,
                elapsed_seconds=elapsed,
                message=f"call did not complete within {timeout_seconds:.0f}s",
                hint=_hint_for(SmoketestKind.TIMEOUT, provider),
            )

        if error_holder:
            elapsed = time.monotonic() - start
            kind, summary = _categorize(error_holder[0])
            return SmoketestResult(
                spec=spec,
                kind=kind,
                elapsed_seconds=elapsed,
                message=f"inference failed: {summary}",
                hint=_hint_for(kind, provider),
                raw_error=str(error_holder[0]),
            )

        elapsed = time.monotonic() - start
        response = response_holder[0] if response_holder else None
        content = getattr(response, "content", "")
        text = str(content)[:60].strip()
        return SmoketestResult(
            spec=spec,
            kind=SmoketestKind.OK,
            elapsed_seconds=elapsed,
            message=f"model replied: {text!r}" if text else "model returned empty body",
            thinking_used=bool(thinking and extra_kwargs.get("thinking"))
            or bool(thinking and extra_kwargs.get("reasoning_effort")),
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        kind, summary = _categorize(exc)
        return SmoketestResult(
            spec=spec,
            kind=kind,
            elapsed_seconds=elapsed,
            message=f"inference failed: {summary}",
            hint=_hint_for(kind, provider),
            raw_error=str(exc),
        )


__all__ = [
    "SmoketestKind",
    "SmoketestResult",
    "smoketest_model",
]
