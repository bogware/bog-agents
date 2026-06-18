"""Live-turn Bedrock resilience: categorize failures + auto-fallback.

The motivating bug: a fresh user with AWS credentials is auto-defaulted
to a heavily access-gated Bedrock model (Claude Opus). The model client
builds fine — no network call happens at construction — so nothing fails
until the first real ``.invoke()`` when the user types "hi". At that
point boto3 raises ``AccessDeniedException``. Crucially, that exception is
raised *inside the langgraph dev server subprocess*, where langgraph's
serde replaces the detail with a generic "An internal error occurred"
before it crosses the SSE boundary. The user sees a bare "internal server
error" ~10-15s later, with none of the rich, actionable diagnosis that
``bog_agents_cli._bedrock`` already knows how to produce.

This middleware closes that gap. It wraps the model call **in-process**,
where the original botocore ``ClientError`` is still live, and:

- For "a different model would work" failures — model access not granted
  to *this* model, *this* model id invalid, or *this* model throttled —
  it descends a fallback ladder (the configured ``[models].fallbacks``
  first, then Claude Opus -> Sonnet -> Haiku -> Amazon Nova), announces
  the switch, and returns the first hittable model's real response. The
  working model is remembered for the rest of the session so the dead
  primary is not re-tried on every subsequent step of the turn.
- For everything else — no region, missing/expired credentials, network,
  package-missing, unknown — and when the ladder is exhausted, it returns
  a synthetic ``AIMessage`` carrying the categorized, region-named
  diagnosis. Because that is *normal stream content* it survives the
  subprocess boundary intact, so the user finally sees "Bedrock model
  access NOT granted ... grant it in the console for region us-east-1"
  instead of "internal server error".

Ordering: attach this OUTSIDE ``BedrockRefreshMiddleware`` (earlier in the
middleware list) so credential-refresh gets first crack at an expired-SSO
failure; only if refresh declines/fails does this middleware produce the
terminal diagnosis. See ``bog_agents_cli.agent.create_cli_agent``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage

from bog_agents_cli._bedrock import (
    BedrockError,
    BedrockErrorKind,
    categorize_bedrock_error,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


# Kinds where a *different model* might succeed where this one failed:
# access not granted to THIS model, THIS model id invalid, or THIS model
# throttled (a different family carries separate quota). Region / credential
# / network / package failures are account-wide — switching models won't
# help, so those get a direct diagnosis instead of a ladder walk.
_FALLBACKABLE_KINDS: frozenset[BedrockErrorKind] = frozenset(
    {
        BedrockErrorKind.MODEL_ACCESS_DENIED,
        BedrockErrorKind.MODEL_ID_INVALID,
        BedrockErrorKind.THROTTLED,
    }
)

# Hard cap on fallback hops so a fully-locked-down account doesn't spend a
# dozen model builds + calls before surfacing the diagnosis. Set high enough
# to walk past the premium tiers (Opus/Sonnet/Haiku + premium Nova) and reach
# the broadly-available Nova Lite/Micro models, since an access-denied probe
# fails fast (Bedrock returns 4xx immediately, no tokens generated).
_DEFAULT_MAX_FALLBACK_HOPS = 6

# Exception type names that signal an AWS/botocore-origin failure. Used to
# avoid converting an unrelated bug (which should keep its traceback) into a
# friendly-but-misleading Bedrock diagnosis.
_AWS_FAILURE_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "ClientError",
        "BotoCoreError",
        "NoCredentialsError",
        "NoRegionError",
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ParamValidationError",
        "ValidationError",
        "TokenRetrievalError",
        "ExpiredTokenException",
        "SSOTokenLoadError",
        "UnauthorizedSSOTokenError",
        "ThrottlingException",
        "AccessDeniedException",
        "ValidationException",
        "ResourceNotFoundException",
    }
)

_AWS_FAILURE_SUBSTRINGS: tuple[str, ...] = (
    "bedrock",
    "botocore",
    "boto3",
    "accessdenied",
    "validationexception",
    "could not connect to the endpoint",
    "unable to locate credentials",
    "aws_default_region",
    "you must specify a region",
    "security token",
    "expiredtoken",
    "throttl",
)

# Providers whose ``provider:model`` specs we recognise so a bare Bedrock id
# (which itself contains ':' in the ``…-v1:0`` suffix) isn't mis-split.
_KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {
        "bedrock",
        "bedrock_converse",
        "anthropic",
        "openai",
        "azure_openai",
        "google_genai",
        "google_vertexai",
        "ollama",
        "mistralai",
        "cohere",
        "groq",
        "deepseek",
        "xai",
        "openrouter",
        "nvidia",
        "together",
        "fireworks",
        "perplexity",
    }
)


# Cross-region inference-profile prefixes (us./eu./apac./jp./sa./global.).
_REGION_PREFIXES: tuple[str, ...] = ("us.", "eu.", "apac.", "jp.", "sa.", "global.")


def _bare_model_id(spec: str) -> str:
    """Return the model id from a ``provider:model`` spec or bare id.

    Bedrock ids embed a colon in their ``…-v1:0`` version suffix, so a naive
    ``split(':')`` would corrupt them. We only strip a leading token when it
    is a recognised provider name.
    """
    s = (spec or "").strip().lower()
    if ":" in s:
        provider, _, rest = s.partition(":")
        if provider in _KNOWN_PROVIDERS and rest:
            return rest
    return s


def _split_region(model_id: str) -> tuple[str, str]:
    """Split a Bedrock id into ``(region_prefix, core)``.

    ``us.anthropic.claude-opus-4-8`` -> ``("us", "anthropic.claude-opus-4-8")``.
    A bare id with no cross-region prefix yields ``("", id)``.
    """
    low = model_id.lower()
    for prefix in _REGION_PREFIXES:
        if low.startswith(prefix):
            return prefix.rstrip("."), model_id[len(prefix) :]
    return "", model_id


def _model_family(core: str) -> str:
    """Coarse model-family key for diversity dedup (region-agnostic).

    ``anthropic.claude-sonnet-4-6`` -> ``anthropic.claude-sonnet``;
    ``amazon.nova-lite-v1:0`` -> ``amazon.nova-lite``;
    ``meta.llama4-maverick-17b-instruct-v1:0`` -> ``meta.llama4-maverick``.
    Used so the fallback ladder prefers a genuinely *different* model rather
    than the same model in another region / at another version.
    """
    c = core.lower()
    vendor, _, model = c.partition(".")
    parts = [p for p in model.split("-") if p]
    family = "-".join(parts[:2]) if parts else model
    return f"{vendor}.{family}"


def _region_prefix(region: str | None) -> str:
    """Map an AWS region to its Bedrock inference-profile prefix.

    ``us-east-1`` -> ``us``, ``eu-west-1`` -> ``eu``, ``ap-northeast-1`` ->
    ``jp``, other ``ap-*`` -> ``apac``, ``sa-*`` -> ``sa``. Unknown / unset
    falls back to ``us`` (broadest profile coverage). Mirrors the resolver in
    ``bog_agents._models``.
    """
    if not region:
        return "us"
    r = region.lower().strip()
    if r.startswith("ap-northeast-1"):
        return "jp"
    if r.startswith("ap"):
        return "apac"
    if r.startswith("eu"):
        return "eu"
    if r.startswith("sa"):
        return "sa"
    return "us"


def _diverse_candidates(region_prefix: str, *, exclude_family: str = "") -> list[str]:
    """One Bedrock id per model family, preferring ``region_prefix``.

    Walks the ``bedrock_converse`` catalog keeping the first id seen for each
    coarse model family, so the result spans Opus/Sonnet/Haiku/Nova/... rather
    than three regions and two versions of the same model. Same-region ids
    claim each family before any cross-region variant.
    """
    try:
        from bog_agents_cli.provider_catalog import get_default_model_candidates

        catalog = [
            str(m).strip() for m in get_default_model_candidates("bedrock_converse")
        ]
    except Exception:
        logger.debug("could not load bedrock catalog candidates", exc_info=True)
        return []

    region_prefix = (region_prefix or "").lower()
    seen: set[str] = set()
    in_region: list[str] = []
    other: list[str] = []

    def _collect(mids: list[str], target: list[str]) -> None:
        for mid in mids:
            family = _model_family(_split_region(mid)[1])
            if exclude_family and family == exclude_family:
                continue
            if family in seen:
                continue
            seen.add(family)
            target.append(mid)

    in_mids = [
        m for m in catalog if region_prefix and _split_region(m)[0] == region_prefix
    ]
    out_mids = [
        m
        for m in catalog
        if not (region_prefix and _split_region(m)[0] == region_prefix)
    ]
    _collect(in_mids, in_region)
    _collect(out_mids, other)
    return in_region + other


def diverse_bedrock_candidates(region: str | None) -> list[str]:
    """Family-deduped Bedrock model ids, preferring the profile region for ``region``.

    Used to seed the first-run probe so it samples Opus -> Sonnet -> Haiku ->
    Nova (one id per family) instead of burning attempts on the same model in
    multiple regions / versions.
    """
    return _diverse_candidates(_region_prefix(region))


def is_bedrock_chat_model(model: Any) -> bool:  # noqa: ANN401 — duck-typed chat model
    """Return whether a resolved chat model is an AWS Bedrock model.

    The CLI resolves a ``provider:model`` string to a ``BaseChatModel``
    *before* middleware is attached, so an ``isinstance(model, str)`` check
    is always False on the live server path. Detect by class name first
    (cheap, no model introspection), then fall back to the LangSmith
    provider tag.
    """
    name = type(model).__name__
    if name in ("ChatBedrock", "ChatBedrockConverse"):
        return True
    try:
        from bog_agents._models import get_model_provider  # noqa: PLC2701

        provider = get_model_provider(model) or ""
    except Exception:
        return False
    return provider in ("amazon_bedrock", "bedrock", "bedrock_converse")


def _current_model_id(model: Any) -> str:  # noqa: ANN401 — duck-typed chat model
    """Best-effort native model id (e.g. ``us.anthropic.claude-opus-4-8``)."""
    try:
        from bog_agents._models import get_model_identifier  # noqa: PLC2701

        return (get_model_identifier(model) or "").strip()
    except Exception:
        return ""


def _looks_like_bedrock_failure(exc: BaseException) -> bool:
    """Heuristically decide whether ``exc`` came from the AWS/Bedrock layer.

    Guards against swallowing an unrelated bug into a friendly Bedrock
    diagnosis: only AWS-shaped exceptions are converted; anything else is
    re-raised so its traceback survives.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        return True
    if type(exc).__name__ in _AWS_FAILURE_TYPE_NAMES:
        return True
    low = str(exc).lower()
    return any(s in low for s in _AWS_FAILURE_SUBSTRINGS)


def bedrock_fallback_specs(current_model_id: str) -> list[str]:
    """Ordered alternate model specs to try when the current model fails.

    The ladder is built for *diversity within a small hop budget* — after,
    say, Opus is access-denied, we want to reach a genuinely different and
    less-gated model (Sonnet, then Haiku, then Amazon Nova) quickly rather
    than burning hops on the same model in other regions / at other versions.

    Order:

    1. ``[models].fallbacks`` from config (explicit user intent, any provider).
    2. Catalog ``bedrock_converse`` candidates in the SAME region as the
       failed model, one per model family, excluding the failed model's own
       family (its access grant / id problem almost certainly applies to its
       other versions too).
    3. The same, for other regions, as a last resort.

    Args:
        current_model_id: The native id of the model that just failed.

    Returns:
        A de-duplicated, ordered list of ``provider:model`` specs.
    """
    config_specs: list[str] = []
    try:
        from bog_agents_cli.model_config import ModelConfig

        config_specs = [
            str(s).strip() for s in ModelConfig.load().fallbacks if str(s).strip()
        ]
    except Exception:
        logger.debug(
            "could not load [models].fallbacks for bedrock ladder", exc_info=True
        )

    cur_region, cur_core = _split_region(_bare_model_id(current_model_id))
    cur_family = _model_family(cur_core) if cur_core else ""
    catalog_specs = [
        f"bedrock_converse:{mid}"
        for mid in _diverse_candidates(cur_region, exclude_family=cur_family)
    ]

    out: list[str] = []
    seen: set[str] = set()
    for spec in (*config_specs, *catalog_specs):
        if not spec or spec in seen:
            continue
        seen.add(spec)
        out.append(spec)
    return out


class BedrockResilienceMiddleware(AgentMiddleware[Any, Any, Any]):
    """Categorize live Bedrock model failures and auto-fall-back.

    Attached automatically by the CLI when a Bedrock model is in use, and
    placed OUTSIDE ``BedrockRefreshMiddleware`` so credential refresh runs
    first. SDK callers can opt in by adding it to their middleware list.

    Args:
        interactive: Whether the session is interactive (reserved for future
            prompts; currently only recorded).
        announce: When True (default), prepend a one-line note to the first
            answer produced after a fallback so the user knows the model was
            swapped. Disable for a silent fallback.
        max_hops: Maximum number of alternate models to try before giving up
            and emitting the diagnosis.
    """

    def __init__(
        self,
        *,
        interactive: bool = True,
        announce: bool = True,
        max_hops: int = _DEFAULT_MAX_FALLBACK_HOPS,
    ) -> None:
        super().__init__()
        self._interactive = interactive
        self._announce = announce
        self._max_hops = max(0, int(max_hops))
        # Once an alternate model works, stick to it for the rest of the
        # session so the dead primary isn't re-tried on every model call of a
        # multi-step turn. Lives for the process (the graph is built once).
        self._sticky_alt: BaseChatModel | None = None

    # -- async path (the live CLI server path) -----------------------------

    async def awrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,  # noqa: ANN401 — langchain middleware contract
    ) -> ModelResponse:
        """Async-wrap the model call: categorize Bedrock failures + fall back.

        Raises:
            KeyboardInterrupt: Propagated unchanged.
            SystemExit: Propagated unchanged.
            asyncio.CancelledError: Propagated unchanged.
        """
        if self._sticky_alt is not None:
            request = request.override(model=self._sticky_alt)
        try:
            return await call_next(request)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:
            handled = await self._ahandle(exc, request, call_next)
            if handled is None:
                raise
            return handled

    async def _ahandle(
        self,
        exc: BaseException,
        request: ModelRequest,
        call_next: Any,  # noqa: ANN401
    ) -> ModelResponse | None:
        if not _looks_like_bedrock_failure(exc):
            return None
        err = self._categorize(exc)
        if err is None:
            return None
        region = self._region()
        current_id = _current_model_id(request.model)
        if err.kind in _FALLBACKABLE_KINDS and self._max_hops > 0:
            tried: list[str] = []
            for spec in bedrock_fallback_specs(current_id)[: self._max_hops]:
                alt = self._build_model(spec)
                if alt is None:
                    continue
                try:
                    resp = await call_next(request.override(model=alt))
                except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                    raise
                except BaseException as alt_exc:
                    tried.append(spec)
                    logger.info("bedrock fallback %s also failed: %s", spec, alt_exc)
                    continue
                self._sticky_alt = alt
                logger.info(
                    "bedrock: fell back from %s to %s", current_id or "primary", spec
                )
                return self._annotate(
                    resp, primary=current_id, used=spec, err=err, region=region
                )
            return self._diagnosis_response(
                err, region, current_id=current_id, tried=tried
            )
        return self._diagnosis_response(err, region, current_id=current_id)

    # -- sync path (tests / non-streaming callers) -------------------------

    def wrap_model_call(  # type: ignore[override]
        self,
        request: ModelRequest,
        call_next: Any,  # noqa: ANN401 — langchain middleware contract
    ) -> ModelResponse:
        """Sync-wrap the model call: categorize Bedrock failures + fall back.

        Raises:
            KeyboardInterrupt: Propagated unchanged.
            SystemExit: Propagated unchanged.
        """
        if self._sticky_alt is not None:
            request = request.override(model=self._sticky_alt)
        try:
            return call_next(request)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            handled = self._handle(exc, request, call_next)
            if handled is None:
                raise
            return handled

    def _handle(
        self,
        exc: BaseException,
        request: ModelRequest,
        call_next: Any,  # noqa: ANN401
    ) -> ModelResponse | None:
        if not _looks_like_bedrock_failure(exc):
            return None
        err = self._categorize(exc)
        if err is None:
            return None
        region = self._region()
        current_id = _current_model_id(request.model)
        if err.kind in _FALLBACKABLE_KINDS and self._max_hops > 0:
            tried: list[str] = []
            for spec in bedrock_fallback_specs(current_id)[: self._max_hops]:
                alt = self._build_model(spec)
                if alt is None:
                    continue
                try:
                    resp = call_next(request.override(model=alt))
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as alt_exc:
                    tried.append(spec)
                    logger.info("bedrock fallback %s also failed: %s", spec, alt_exc)
                    continue
                self._sticky_alt = alt
                logger.info(
                    "bedrock: fell back from %s to %s", current_id or "primary", spec
                )
                return self._annotate(
                    resp, primary=current_id, used=spec, err=err, region=region
                )
            return self._diagnosis_response(
                err, region, current_id=current_id, tried=tried
            )
        return self._diagnosis_response(err, region, current_id=current_id)

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _categorize(exc: BaseException) -> BedrockError | None:
        try:
            return categorize_bedrock_error(exc)
        except Exception:
            logger.debug("bedrock categorization failed", exc_info=True)
            return None

    @staticmethod
    def _region() -> str | None:
        try:
            from bog_agents_cli.model_config import resolve_aws_region

            return resolve_aws_region(fallback="us-east-1")
        except Exception:
            return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

    @staticmethod
    def _build_model(spec: str) -> BaseChatModel | None:
        try:
            from bog_agents_cli.config import create_model

            return create_model(spec).model
        except Exception:
            logger.info("bedrock fallback: could not build %s", spec, exc_info=True)
            return None

    def _diagnosis_response(
        self,
        err: BedrockError,
        region: str | None,
        *,
        current_id: str = "",
        tried: Sequence[str] = (),
    ) -> ModelResponse:
        text = self._diagnosis_text(err, region, current_id=current_id, tried=tried)
        msg = AIMessage(
            content=text,
            response_metadata={"bog_agents_bedrock_diagnosis": err.kind.value},
        )
        return ModelResponse(result=[msg])

    @staticmethod
    def _diagnosis_text(
        err: BedrockError,
        region: str | None,
        *,
        current_id: str = "",
        tried: Sequence[str] = (),
    ) -> str:
        parts: list[str] = [err.banner().rstrip("\n")]
        if current_id:
            parts.append(f"[model]  {current_id}")
        if region:
            parts.append(f"[region] Bedrock used AWS region: {region}")
            if err.kind == BedrockErrorKind.MODEL_ACCESS_DENIED:
                parts.append(
                    f"         Model access is per-region — grant it for {region} "
                    "in the console, or set AWS_REGION to a region where you "
                    "already have access."
                )
        if tried:
            parts.append("[fallback] also tried (all failed): " + ", ".join(tried))
        parts.append(
            "Next: run `bog-agents test-bedrock` for a full probe, or `/model` "
            "to switch to a model you can reach."
        )
        return "\n".join(parts)

    def _annotate(
        self,
        resp: ModelResponse,
        *,
        primary: str,
        used: str,
        err: BedrockError,
        region: str | None,
    ) -> ModelResponse:
        if not self._announce:
            return resp
        try:
            note = self._switch_note(primary=primary, used=used, err=err, region=region)
            new_result = list(resp.result)
            for i, m in enumerate(new_result):
                if isinstance(m, AIMessage):
                    new_result[i] = self._prepend_note(m, note)
                    break
            else:
                new_result.insert(0, AIMessage(content=note))
            return ModelResponse(
                result=new_result, structured_response=resp.structured_response
            )
        except Exception:
            logger.debug("bedrock fallback annotation failed", exc_info=True)
            return resp

    @staticmethod
    def _switch_note(
        *, primary: str, used: str, err: BedrockError, region: str | None
    ) -> str:
        region_suffix = f" in {region}" if region else ""
        used_id = _bare_model_id(used)
        primary_label = primary or "the configured model"
        # ASCII-only: this string may be written to a cp1252 console / log on
        # non-en-US Windows; a stray non-ASCII glyph there raises (see the
        # encoding guidance in CLAUDE.md).
        return (
            f"_Bedrock note: `{primary_label}` was not usable "
            f"({err.title.lower()}{region_suffix}); answered with `{used_id}` "
            "instead. Use `/model` to set a permanent choice._\n\n"
        )

    @staticmethod
    def _prepend_note(msg: AIMessage, note: str) -> AIMessage:
        content = msg.content
        if isinstance(content, str):
            return msg.model_copy(update={"content": note + content})
        if isinstance(content, list):
            return msg.model_copy(
                update={"content": [{"type": "text", "text": note}, *content]}
            )
        return msg


__all__ = [
    "BedrockResilienceMiddleware",
    "bedrock_fallback_specs",
    "is_bedrock_chat_model",
]
