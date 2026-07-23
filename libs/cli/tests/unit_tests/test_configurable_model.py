"""Tests for ConfigurableModelMiddleware."""

import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from bog_agents_cli.configurable_model import (
    CLIContext,
    ConfigurableModelMiddleware,
    _is_anthropic_model,
    _is_ollama_model,
)


def _make_model(name: str) -> MagicMock:
    """Create a mock BaseChatModel with model_name set."""
    model = MagicMock(spec=BaseChatModel)
    model.model_name = name
    model.model_dump.return_value = {"model_name": name}
    return model


def _make_request(
    model: BaseChatModel,
    context: CLIContext | None = None,
    model_settings: dict[str, Any] | None = None,
) -> ModelRequest:
    """Create a ModelRequest with a runtime that carries CLIContext."""
    runtime = SimpleNamespace(context=context)
    return ModelRequest(
        model=model,
        messages=[HumanMessage(content="hi")],
        tools=[],
        runtime=cast("Any", runtime),
        model_settings=model_settings,
    )


def _make_response() -> ModelResponse[Any]:
    """Create a minimal model response for handler mocks."""
    return ModelResponse(result=[AIMessage(content="response")])


def _system_text(request: ModelRequest) -> str:
    """Extract a string representation of the system prompt for assertions."""
    system_message = request.system_message
    if system_message is None:
        return ""
    return (
        f"{system_message.content!s} {getattr(system_message, 'content_blocks', '')!s}"
    )


_mw = ConfigurableModelMiddleware()


class TestNoOverride:
    """Cases where the middleware should pass the request through unchanged."""

    def test_no_context(self) -> None:
        request = _make_request(_make_model("claude-sonnet-4-6"), context=None)
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0].model is request.model

    def test_empty_context(self) -> None:
        request = _make_request(_make_model("claude-sonnet-4-6"), context=CLIContext())
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0] is request

    def test_same_model_spec(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="claude-sonnet-4-6"),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0] is request

    def test_provider_prefixed_spec_matches(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="anthropic:claude-sonnet-4-6"),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0] is request

    def test_none_runtime(self) -> None:
        request = ModelRequest(
            model=_make_model("claude-sonnet-4-6"),
            messages=[HumanMessage(content="hi")],
            tools=[],
            runtime=None,
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0].model is request.model

    def test_non_dict_context_ignored(self) -> None:
        runtime = SimpleNamespace(context="not-a-dict")
        request = ModelRequest(
            model=_make_model("claude-sonnet-4-6"),
            messages=[HumanMessage(content="hi")],
            tools=[],
            runtime=cast("Any", runtime),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0].model is request.model

    def test_empty_model_params(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model_params={}),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )
        assert captured[0] is request


class TestRuntimeWorkflowControls:
    """Runtime workflow controls beyond model selection."""

    def test_effort_level_merges_reasoning_defaults(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(effort_level="max"),
            model_settings={"top_p": 0.9},
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        # Non-introspectable mock => non-reasoning fallback: temperature only,
        # never an output cap (RD-1 / v4).
        assert "max_tokens" not in captured[0].model_settings
        assert captured[0].model_settings["temperature"] == 1.0
        assert captured[0].model_settings["top_p"] == 0.9

    def test_model_params_override_effort_defaults(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(
                effort_level="high",
                model_params={"temperature": 0.2},
            ),
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert "max_tokens" not in captured[0].model_settings
        assert captured[0].model_settings["temperature"] == 0.2

    def test_plan_mode_appends_system_prompt_and_filters_tools(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(plan_mode=True),
        )
        request = request.override(
            tools=[
                cast("Any", SimpleNamespace(name="read_file")),
                cast("Any", SimpleNamespace(name="write_file")),
            ]
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert "Plan Mode Active" in _system_text(captured[0])
        assert [tool.name for tool in captured[0].tools] == ["read_file"]

    def test_profile_prompt_is_appended_to_system_message(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(system_prompt_append="Follow the review workflow."),
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert "Follow the review workflow." in _system_text(captured[0])

    def test_effort_level_leaves_ollama_output_uncapped(self) -> None:
        # RD-1 / v4: effort no longer sets num_predict/max_tokens on a
        # non-reasoning model; only temperature is nudged (and moved onto the
        # ChatOllama instance). The max_tokens->num_predict aliasing itself is
        # still covered by test_ollama_model_params_normalize_max_tokens.
        ollama_mod = pytest.importorskip("langchain_ollama")

        request = _make_request(
            ollama_mod.ChatOllama(model="deepseek-coder:6.7b"),
            context=CLIContext(effort_level="high"),
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert captured[0].model_settings == {}
        assert captured[0].model.temperature == 0.7  # ty: ignore[unresolved-attribute]


class TestNativeReasoningEffort:
    """Effort is translated onto each provider's real reasoning knob."""

    def test_anthropic_reasoning_model_gets_native_params_not_max_tokens(self) -> None:
        # The runtime spec resolves the model without a swap (identifier match),
        # so the effort translation runs against `anthropic:claude-opus-4-8`.
        request = _make_request(
            _make_model("claude-opus-4-8"),
            context=CLIContext(model="anthropic:claude-opus-4-8", effort_level="low"),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        settings = captured[0].model_settings
        assert settings["output_config"] == {"effort": "low"}
        assert settings["thinking"] == {"type": "adaptive", "display": "summarized"}
        # The central regression: never cap a reasoning model's output.
        assert "max_tokens" not in settings
        assert "temperature" not in settings

    def test_openai_reasoning_model_gets_reasoning_effort(self) -> None:
        request = _make_request(
            _make_model("gpt-5.6"),
            context=CLIContext(model="openai:gpt-5.6", effort_level="high"),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        settings = captured[0].model_settings
        assert settings["reasoning"] == {"effort": "high", "summary": "auto"}
        assert "max_tokens" not in settings

    def test_reasoning_model_unsupported_level_leaves_default(self) -> None:
        # `max` is not accepted by Opus 4.5 — emit nothing rather than cap.
        request = _make_request(
            _make_model("claude-opus-4-5"),
            context=CLIContext(model="anthropic:claude-opus-4-5", effort_level="max"),
            model_settings={"top_p": 0.9},
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        settings = captured[0].model_settings
        assert "max_tokens" not in settings
        assert "output_config" not in settings
        assert settings["top_p"] == 0.9

    def test_non_reasoning_model_gets_temperature_preset_no_cap(self) -> None:
        # A non-reasoning model gets the legacy preset — but temperature only,
        # never a max_tokens cap that would truncate its output (RD-1 / v4).
        request = _make_request(
            _make_model("gpt-4o"),
            context=CLIContext(model="openai:gpt-4o", effort_level="low"),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        settings = captured[0].model_settings
        assert "max_tokens" not in settings
        assert settings["temperature"] == 0.3


class TestModelSwap:
    """Cases where the middleware should swap the model."""

    def test_different_model_swapped(self) -> None:
        original = _make_model("claude-sonnet-4-6")
        override = _make_model("gpt-4o")
        request = _make_request(original, context=CLIContext(model="openai:gpt-4o"))

        captured: list[ModelRequest] = []
        with patch(
            "bog_agents_cli.configurable_model.resolve_model", return_value=override
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert captured[0].model is override
        assert request.model is original  # original unchanged

    async def test_async_model_swapped(self) -> None:
        original = _make_model("claude-sonnet-4-6")
        override = _make_model("gpt-4o")
        request = _make_request(original, context=CLIContext(model="openai:gpt-4o"))

        captured: list[ModelRequest] = []

        async def handler(r: ModelRequest) -> ModelResponse[Any]:
            captured.append(r)
            return _make_response()

        with patch(
            "bog_agents_cli.configurable_model.resolve_model", return_value=override
        ):
            await _mw.awrap_model_call(request, handler)

        assert captured[0].model is override


class TestAnthropicSettingsStripped:
    """Anthropic-specific model_settings stripped on cross-provider swap.

    When swapping from Anthropic to a non-Anthropic model, provider-specific
    settings like `cache_control` must be stripped to avoid TypeError on the
    target provider's API (e.g. OpenAI/Groq).
    """

    def test_cache_control_stripped_on_swap(self) -> None:
        override = _make_model("gpt-4o")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="openai:gpt-4o"),
            model_settings={"cache_control": {"type": "ephemeral", "ttl": "5m"}},
        )
        captured: list[ModelRequest] = []
        with (
            patch(
                "bog_agents_cli.configurable_model.resolve_model", return_value=override
            ),
            patch(
                "bog_agents_cli.configurable_model._is_anthropic_model",
                return_value=False,
            ),
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert "cache_control" not in captured[0].model_settings

    def test_cache_control_preserved_for_anthropic_swap(self) -> None:
        override = _make_model("claude-opus-4-6")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="anthropic:claude-opus-4-6"),
            model_settings={"cache_control": {"type": "ephemeral", "ttl": "5m"}},
        )
        captured: list[ModelRequest] = []
        with (
            patch(
                "bog_agents_cli.configurable_model.resolve_model", return_value=override
            ),
            patch(
                "bog_agents_cli.configurable_model._is_anthropic_model",
                return_value=True,
            ),
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert captured[0].model_settings["cache_control"] == {
            "type": "ephemeral",
            "ttl": "5m",
        }

    def test_other_settings_preserved_on_swap(self) -> None:
        override = _make_model("gpt-4o")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="openai:gpt-4o"),
            model_settings={
                "cache_control": {"type": "ephemeral"},
                "max_tokens": 2048,
            },
        )
        captured: list[ModelRequest] = []
        with (
            patch(
                "bog_agents_cli.configurable_model.resolve_model", return_value=override
            ),
            patch(
                "bog_agents_cli.configurable_model._is_anthropic_model",
                return_value=False,
            ),
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert captured[0].model_settings == {"max_tokens": 2048}

    async def test_async_cache_control_stripped(self) -> None:
        override = _make_model("gpt-4o")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="openai:gpt-4o"),
            model_settings={"cache_control": {"type": "ephemeral"}},
        )
        captured: list[ModelRequest] = []

        async def handler(r: ModelRequest) -> ModelResponse[Any]:
            captured.append(r)
            return _make_response()

        with (
            patch(
                "bog_agents_cli.configurable_model.resolve_model", return_value=override
            ),
            patch(
                "bog_agents_cli.configurable_model._is_anthropic_model",
                return_value=False,
            ),
        ):
            await _mw.awrap_model_call(request, handler)

        assert "cache_control" not in captured[0].model_settings

    def test_swap_with_model_params_and_cache_control(self) -> None:
        """Stripping operates on the merged settings, not the original."""
        override = _make_model("gpt-4o")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(
                model="openai:gpt-4o",
                model_params={"temperature": 0.7},
            ),
            model_settings={
                "cache_control": {"type": "ephemeral"},
                "max_tokens": 2048,
            },
        )
        captured: list[ModelRequest] = []
        with (
            patch(
                "bog_agents_cli.configurable_model.resolve_model", return_value=override
            ),
            patch(
                "bog_agents_cli.configurable_model._is_anthropic_model",
                return_value=False,
            ),
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert captured[0].model_settings == {
            "max_tokens": 2048,
            "temperature": 0.7,
        }

    def test_only_cache_control_results_in_empty_settings(self) -> None:
        override = _make_model("gpt-4o")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model="openai:gpt-4o"),
            model_settings={"cache_control": {"type": "ephemeral"}},
        )
        captured: list[ModelRequest] = []
        with (
            patch(
                "bog_agents_cli.configurable_model.resolve_model", return_value=override
            ),
            patch(
                "bog_agents_cli.configurable_model._is_anthropic_model",
                return_value=False,
            ),
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert captured[0].model_settings == {}


class TestIsAnthropicModel:
    """Direct tests for the `_is_anthropic_model` helper."""

    def test_returns_true_for_anthropic(self) -> None:
        from langchain_anthropic import ChatAnthropic

        model = ChatAnthropic(model_name="claude-sonnet-4-6")
        assert _is_anthropic_model(model) is True

    def test_returns_false_for_non_anthropic(self) -> None:
        assert _is_anthropic_model(_make_model("gpt-4o")) is False

    def test_returns_false_when_import_missing(self) -> None:
        with patch.dict("sys.modules", {"langchain_anthropic": None}):
            assert _is_anthropic_model(_make_model("anything")) is False


class TestModelParams:
    """Cases where model_params are merged into model_settings."""

    def test_params_merged(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model_params={"temperature": 0.7}),
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert captured[0].model is request.model
        assert captured[0].model_settings == {"temperature": 0.7}

    def test_params_merge_preserves_existing(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model_params={"temperature": 0.5}),
            model_settings={"max_tokens": 2048},
        )
        captured: list[ModelRequest] = []
        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert captured[0].model_settings == {"max_tokens": 2048, "temperature": 0.5}

    def test_params_with_model_swap(self) -> None:
        override = _make_model("gpt-4o")
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(
                model="openai:gpt-4o", model_params={"max_tokens": 1024}
            ),
        )
        captured: list[ModelRequest] = []
        with patch(
            "bog_agents_cli.configurable_model.resolve_model", return_value=override
        ):
            _mw.wrap_model_call(
                request, lambda r: (captured.append(r), _make_response())[1]
            )

        assert captured[0].model is override
        assert captured[0].model_settings == {"max_tokens": 1024}

    async def test_async_params(self) -> None:
        request = _make_request(
            _make_model("claude-sonnet-4-6"),
            context=CLIContext(model_params={"temperature": 0.3}),
        )
        captured: list[ModelRequest] = []

        async def handler(r: ModelRequest) -> ModelResponse[Any]:
            captured.append(r)
            return _make_response()

        await _mw.awrap_model_call(request, handler)
        assert captured[0].model_settings == {"temperature": 0.3}

    def test_ollama_model_params_normalize_max_tokens(self) -> None:
        ollama_mod = pytest.importorskip("langchain_ollama")

        request = _make_request(
            ollama_mod.ChatOllama(model="deepseek-coder:6.7b"),
            context=CLIContext(model_params={"max_tokens": 1024, "temperature": 0.2}),
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert captured[0].model_settings == {}
        assert captured[0].model.num_predict == 1024  # ty: ignore[unresolved-attribute]
        assert captured[0].model.temperature == 0.2  # ty: ignore[unresolved-attribute]

    def test_ollama_explicit_num_predict_wins_over_alias(self) -> None:
        ollama_mod = pytest.importorskip("langchain_ollama")

        request = _make_request(
            ollama_mod.ChatOllama(model="deepseek-coder:6.7b"),
            context=CLIContext(
                effort_level="medium",
                model_params={"num_predict": 2048},
            ),
        )
        captured: list[ModelRequest] = []

        _mw.wrap_model_call(
            request, lambda r: (captured.append(r), _make_response())[1]
        )

        assert captured[0].model_settings == {}
        assert captured[0].model.num_predict == 2048  # ty: ignore[unresolved-attribute]


class TestIsOllamaModel:
    """Direct tests for the `_is_ollama_model` helper."""

    def test_returns_true_for_ollama(self) -> None:
        ollama_mod = pytest.importorskip("langchain_ollama")

        model = ollama_mod.ChatOllama(model="deepseek-coder:6.7b")
        assert _is_ollama_model(model) is True

    def test_returns_false_for_non_ollama(self) -> None:
        assert _is_ollama_model(_make_model("gpt-4o")) is False

    def test_returns_false_when_import_missing(self) -> None:
        with patch.dict("sys.modules", {"langchain_ollama": None}):
            assert _is_ollama_model(_make_model("anything")) is False
