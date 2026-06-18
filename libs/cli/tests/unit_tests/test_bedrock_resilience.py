"""Tests for live-turn Bedrock resilience: categorization + auto-fallback.

These lock in the fix for the fresh-user "internal server error" symptom: a
Bedrock model failure on a live turn must surface a categorized, region-named
diagnosis (or transparently fall back to a hittable model) instead of an
opaque error. No real AWS calls are made — botocore ``ClientError`` shapes are
faked, and the model call is a local callable.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from bog_agents_cli._bedrock import BedrockErrorKind, pick_hittable_bedrock_model
from bog_agents_cli.bedrock_resilience import (
    BedrockResilienceMiddleware,
    _bare_model_id,
    _model_family,
    bedrock_fallback_specs,
    is_bedrock_chat_model,
)


class _FakeClientError(Exception):
    """Stand-in for a botocore ``ClientError`` (carries a ``.response``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class _FakeModel:
    """Minimal chat-model stand-in exposing ``model_dump`` for id extraction."""

    def __init__(self, model_id: str) -> None:
        self._id = model_id

    def model_dump(self) -> dict[str, str]:
        return {"model_name": self._id}


class _FakeRequest:
    """Minimal ``ModelRequest`` stand-in: just ``.model`` + ``.override``."""

    def __init__(self, model: object) -> None:
        self.model = model

    def override(self, *, model: object = None, **_kw: object) -> _FakeRequest:
        return _FakeRequest(model if model is not None else self.model)


_OPUS = "us.anthropic.claude-opus-4-8"
_SONNET = "us.anthropic.claude-sonnet-4-6"


def _access_denied() -> _FakeClientError:
    return _FakeClientError(
        "AccessDeniedException",
        "You don't have access to the model with the specified model ID.",
    )


@pytest.fixture
def patched_mw(monkeypatch: pytest.MonkeyPatch) -> BedrockResilienceMiddleware:
    """A middleware whose region is fixed and whose model builder is faked.

    Keeps the ladder offline: ``_build_model`` returns a ``_FakeModel`` tagged
    with the bare id, so the test's ``call_next`` can tell which rung it is on.
    """
    monkeypatch.setattr(
        BedrockResilienceMiddleware,
        "_region",
        staticmethod(lambda: "us-east-1"),
    )
    monkeypatch.setattr(
        BedrockResilienceMiddleware,
        "_build_model",
        staticmethod(lambda spec: _FakeModel(_bare_model_id(spec))),
    )
    return BedrockResilienceMiddleware(interactive=False)


class TestHelpers:
    def test_is_bedrock_chat_model_by_class_name(self) -> None:
        class ChatBedrockConverse:
            pass

        assert is_bedrock_chat_model(ChatBedrockConverse()) is True

    def test_is_bedrock_chat_model_false_for_other(self) -> None:
        assert (
            is_bedrock_chat_model(_FakeModel("us.anthropic.claude-opus-4-8")) is False
        )

    def test_bare_model_id_preserves_version_colon(self) -> None:
        # The ``…-v1:0`` suffix must NOT be treated as a provider separator.
        spec = "bedrock_converse:us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert _bare_model_id(spec) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_model_family_groups_versions(self) -> None:
        assert _model_family("anthropic.claude-sonnet-4-6") == "anthropic.claude-sonnet"
        assert _model_family("amazon.nova-lite-v1:0") == "amazon.nova-lite"


class TestFallbackLadder:
    def test_ladder_is_diverse_and_excludes_failed_family(self) -> None:
        ladder = bedrock_fallback_specs(_OPUS)
        # Never retries the same model family in another region/version.
        assert all("claude-opus" not in spec for spec in ladder)
        # Leads with a different Claude tier, same region.
        assert ladder[0] == f"bedrock_converse:{_SONNET}"
        # Reaches a broadly-available Amazon Nova model within the hop budget.
        assert any("amazon.nova" in spec for spec in ladder[:6])

    def test_ladder_prefers_same_region(self) -> None:
        ladder = bedrock_fallback_specs("eu.anthropic.claude-opus-4-8")
        # Same-region (eu.) alternates come before any cross-region ones.
        first = _bare_model_id(ladder[0])
        assert first.startswith("eu."), ladder


class TestSyncResilience:
    def test_access_denied_falls_back_and_announces(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        seen: list[str] = []

        def call_next(req: _FakeRequest) -> ModelResponse:
            mid = req.model._id
            seen.append(mid)
            if "opus" in mid:
                raise _access_denied()
            return ModelResponse(result=[AIMessage(content=f"hi from {mid}")])

        result = patched_mw.wrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
        )
        assert isinstance(result, ModelResponse)
        content = result.result[0].content
        assert f"hi from {_SONNET}" in content  # answered by the fallback model
        assert "Bedrock" in content and _SONNET in content  # announced the switch
        # Primary tried once, then exactly one fallback hop succeeded.
        assert seen[0] == _OPUS
        assert seen[1] == _SONNET

    def test_access_denied_all_fail_yields_diagnosis(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        def call_next(req: _FakeRequest) -> ModelResponse:
            raise _access_denied()

        result = patched_mw.wrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
        )
        msg = result.result[0]
        assert isinstance(msg, AIMessage)
        text = msg.content.lower()
        assert "access" in text  # the real cause, not "internal server error"
        assert "us-east-1" in text  # region named (per the region decision)
        assert msg.response_metadata.get("bog_agents_bedrock_diagnosis") == (
            BedrockErrorKind.MODEL_ACCESS_DENIED.value
        )

    def test_region_error_is_diagnosed_not_swapped(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        calls = {"n": 0}

        def call_next(req: _FakeRequest) -> ModelResponse:
            calls["n"] += 1
            raise Exception("You must specify a region.")  # noqa: TRY002

        result = patched_mw.wrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
        )
        # Account-wide failure: no model swap attempted, just a diagnosis.
        assert calls["n"] == 1
        text = result.result[0].content.lower()
        assert "region" in text

    def test_expired_credentials_diagnosed(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        def call_next(req: _FakeRequest) -> ModelResponse:
            raise Exception(  # noqa: TRY002
                "The security token included in the request is expired"
            )

        result = patched_mw.wrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
        )
        text = result.result[0].content.lower()
        assert "expired" in text or "sso login" in text

    def test_non_bedrock_error_propagates(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        # An unrelated bug must keep its traceback, not be swallowed into a
        # friendly-but-wrong Bedrock diagnosis.
        def call_next(req: _FakeRequest) -> ModelResponse:
            raise ValueError("totally unrelated programming bug")

        with pytest.raises(ValueError, match="unrelated programming bug"):
            patched_mw.wrap_model_call(
                request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
            )

    def test_keyboard_interrupt_propagates(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        def call_next(req: _FakeRequest) -> ModelResponse:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            patched_mw.wrap_model_call(
                request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
            )

    def test_fallback_is_sticky_for_session(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        def first_turn(req: _FakeRequest) -> ModelResponse:
            if "opus" in req.model._id:
                raise _access_denied()
            return ModelResponse(result=[AIMessage(content="ok")])

        patched_mw.wrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=first_turn
        )

        # Second turn: the dead primary must NOT be re-tried — the working
        # alternate is used directly.
        used: list[str] = []

        def second_turn(req: _FakeRequest) -> ModelResponse:
            used.append(req.model._id)
            return ModelResponse(result=[AIMessage(content="ok2")])

        patched_mw.wrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=second_turn
        )
        assert used == [_SONNET]


class TestAsyncResilience:
    async def test_async_access_denied_falls_back(
        self, patched_mw: BedrockResilienceMiddleware
    ) -> None:
        seen: list[str] = []

        async def call_next(req: _FakeRequest) -> ModelResponse:
            mid = req.model._id
            seen.append(mid)
            if "opus" in mid:
                raise _access_denied()
            return ModelResponse(result=[AIMessage(content=f"hi from {mid}")])

        result = await patched_mw.awrap_model_call(
            request=_FakeRequest(_FakeModel(_OPUS)), call_next=call_next
        )
        assert f"hi from {_SONNET}" in result.result[0].content
        assert seen[0] == _OPUS


def _install_fake_boto3(monkeypatch: pytest.MonkeyPatch, runtime: object) -> None:
    """Inject a fake ``boto3`` module whose ``client()`` returns ``runtime``.

    ``pick_hittable_bedrock_model`` does ``import boto3`` internally, but boto3
    is only present with the ``[bedrock]`` extra (not in the base test env / CI).
    Injecting a stub into ``sys.modules`` lets these tests run anywhere and also
    overrides a real boto3 when present.
    """
    fake = types.ModuleType("boto3")
    fake.client = lambda *_a, **_k: runtime  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)


class TestPickHittableModel:
    def test_returns_first_invokable_candidate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        granted = {"us.amazon.nova-lite-v1:0"}
        calls: list[str] = []

        class _FakeRuntime:
            def converse(self, *, modelId: str, **_kw: Any) -> dict[str, Any]:  # noqa: N803
                calls.append(modelId)
                if modelId in granted:
                    return {"usage": {"totalTokens": 1}}
                raise _access_denied()

        _install_fake_boto3(monkeypatch, _FakeRuntime())
        picked, err = pick_hittable_bedrock_model(
            ["us.anthropic.claude-opus-4-8", "us.amazon.nova-lite-v1:0"],
            "us-east-1",
        )
        assert picked == "us.amazon.nova-lite-v1:0"
        assert err is None
        assert calls == ["us.anthropic.claude-opus-4-8", "us.amazon.nova-lite-v1:0"]

    def test_account_wide_failure_aborts_early(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        class _FakeRuntime:
            def converse(self, *, modelId: str, **_kw: Any) -> dict[str, Any]:  # noqa: N803
                calls.append(modelId)
                raise Exception("Unable to locate credentials")  # noqa: TRY002

        _install_fake_boto3(monkeypatch, _FakeRuntime())
        picked, err = pick_hittable_bedrock_model(
            ["us.anthropic.claude-opus-4-8", "us.amazon.nova-lite-v1:0"],
            "us-east-1",
        )
        assert picked is None
        assert err is not None
        assert err.kind == BedrockErrorKind.CREDENTIALS_MISSING
        # Stopped after the first probe — no point trying more models.
        assert calls == ["us.anthropic.claude-opus-4-8"]


class TestCatalogRefresh:
    def test_bedrock_leads_with_current_opus(self) -> None:
        from bog_agents_cli.provider_catalog import DEFAULT_MODEL_CANDIDATES

        for provider in ("bedrock", "bedrock_converse"):
            ids = list(DEFAULT_MODEL_CANDIDATES[provider])
            assert ids[0] == "us.anthropic.claude-opus-4-8", provider
            # 4-7 retained as a probe fallback (its Bedrock-id validity vs 4-8
            # is account-dependent; the probe self-corrects either way), but
            # after the current 4-8.
            joined = " ".join(ids)
            assert "claude-opus-4-7" in joined
            assert joined.index("claude-opus-4-8") < joined.index("claude-opus-4-7")

    def test_diverse_candidates_one_per_family(self) -> None:
        from bog_agents_cli.bedrock_resilience import (
            _model_family,
            _split_region,
            diverse_bedrock_candidates,
        )

        cands = diverse_bedrock_candidates("us-east-1")
        families = [_model_family(_split_region(c)[1]) for c in cands]
        assert len(families) == len(set(families))  # no family repeated
        # Opus first (Claude-first), Nova reachable within the probe budget.
        assert cands[0] == "us.anthropic.claude-opus-4-8"
        assert any("amazon.nova" in c for c in cands[:8])
