"""Hardening tests for ThinkingMiddleware provider detection (P22).

Bedrock-hosted Claude exposes cross-region inference-profile ids such as
`us.anthropic.claude-opus-4-7`. The previous `_detect_provider` used
`name.startswith("claude")`, which returned 'unknown' for those prefixed ids.
Because `_model_supports_native_thinking` matches by substring it still took
the native branch, so `_bind_thinking_params` bound nothing — `/think`
silently degraded to neither native extended thinking nor the fallback CoT
prompt for every Bedrock Claude user.

These tests pin the fix: prefixed Anthropic ids classify as 'anthropic',
native thinking params are actually bound, and the openai/gemini matchers stay
tight enough to avoid collisions.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from bog_agents.middleware.thinking import (
    ThinkingMiddleware,
    _detect_provider,
    _model_supports_native_thinking,
)

try:
    from langchain.agents.middleware.types import ModelRequest
except ImportError:  # pragma: no cover - import-path fallback
    from langchain.agents.middleware import ModelRequest  # type: ignore[no-redef,attr-defined]


# ---------------------------------------------------------------------------
# _detect_provider — Bedrock prefixed ids must classify as 'anthropic'
# ---------------------------------------------------------------------------


def test_detect_provider_bedrock_prefixed_claude_is_anthropic() -> None:
    # The core P22 regression: cross-region inference-profile ids.
    assert _detect_provider("us.anthropic.claude-opus-4-7") == "anthropic"
    assert _detect_provider("eu.anthropic.claude-sonnet-4-5") == "anthropic"
    assert _detect_provider("apac.anthropic.claude-haiku-4-5") == "anthropic"
    # Bare bedrock and plain forms still work.
    assert _detect_provider("anthropic.claude-opus-4-7") == "anthropic"
    assert _detect_provider("claude-opus-4-7") == "anthropic"
    assert _detect_provider("claude-3-7-sonnet-20250219") == "anthropic"


def test_detect_provider_keeps_openai_and_gemini_tight() -> None:
    # No collision: the anthropic substring check must not swallow these.
    assert _detect_provider("gpt-5") == "openai"
    assert _detect_provider("o1-preview") == "openai"
    assert _detect_provider("o3-mini") == "openai"
    assert _detect_provider("gemini-2.5-pro") == "google"
    assert _detect_provider("models/gemini-2.5-flash") == "google"
    # An unrelated id with an embedded 'o1'/'o3' must NOT be mis-tagged openai
    # (these matchers stay prefix-anchored, not substring).
    assert _detect_provider("mistral-large-2411") == "unknown"
    assert _detect_provider("nova-pro-o1-variant") == "unknown"


def test_native_thinking_still_detected_for_prefixed_claude() -> None:
    # The native-thinking gate is substring-based, so the prefixed Bedrock id
    # must still be recognised — otherwise we'd fall through to the CoT prompt.
    assert _model_supports_native_thinking("us.anthropic.claude-opus-4-7") is True
    assert _model_supports_native_thinking("eu.anthropic.claude-sonnet-4-5") is True


# ---------------------------------------------------------------------------
# End-to-end: native thinking params are actually bound for Bedrock Claude
# ---------------------------------------------------------------------------


class _BedrockClaudeModel:
    """Stub chat model that reports a cross-region Bedrock Claude id."""

    _llm_type = "fake-bedrock"

    def __init__(self) -> None:
        self.bound_kwargs: dict[str, Any] | None = None

    def _get_ls_params(self, **_kwargs: Any) -> dict[str, str]:
        return {"ls_provider": "bedrock_converse", "ls_model_name": "us.anthropic.claude-opus-4-7"}

    def model_dump(self) -> dict[str, Any]:
        # get_model_identifier reads model_name / model from the dump.
        return {"model_name": "us.anthropic.claude-opus-4-7"}

    def bind(self, **kwargs: Any) -> _BedrockClaudeModel:
        self.bound_kwargs = kwargs
        return self


def _make_request(model: Any) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="base system prompt"),
        tools=[],
        runtime=None,
        state={"messages": [HumanMessage(content="hi")]},
    )


def _capture(request: ModelRequest) -> AIMessage:
    _capture.last_request = request  # type: ignore[attr-defined]
    return AIMessage(content="ok")


def test_bedrock_claude_binds_native_thinking_params() -> None:
    # Before P22 this silently bound nothing (provider == 'unknown').
    model = _BedrockClaudeModel()
    mw = ThinkingMiddleware(enabled=True, budget_tokens=12_345)
    mw.wrap_model_call(_make_request(model), _capture)

    seen = _capture.last_request  # type: ignore[attr-defined]
    bound = seen.model
    assert isinstance(bound, _BedrockClaudeModel)
    assert bound.bound_kwargs is not None, "native thinking params were not bound"
    assert bound.bound_kwargs["thinking"] == {"type": "enabled", "budget_tokens": 12_345}
    # System prompt must be untouched (we did NOT fall through to the CoT
    # fallback for a model that supports native thinking).
    sm = seen.system_message
    assert sm is not None and sm.content == "base system prompt"
