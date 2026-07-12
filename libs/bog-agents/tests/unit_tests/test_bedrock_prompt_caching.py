"""Bedrock prompt-caching wiring for `create_agent`.

A Bedrock model otherwise pays full input-token price on every turn because
`AnthropicPromptCachingMiddleware` ignores non-Anthropic providers. `graph.py`
now appends `BedrockPromptCachingMiddleware` (from the optional `langchain-aws`
package) directly after the Anthropic caching entry whenever the resolved model
targets AWS Bedrock. These tests pin:

- the Bedrock entry is appended (after Anthropic) for a Bedrock model, on the
  main agent, the general-purpose subagent, and an explicit Bedrock subagent;
- a NON-Bedrock model's stack is byte-identical (no Bedrock entry, Anthropic
  stays strictly last, and the Bedrock helper is never even consulted);
- the wiring degrades gracefully (no crash, no caching) when `langchain-aws`
  is absent, while an unrelated ImportError still propagates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage

from bog_agents import create_agent, graph as graph_module

from .chat_model import GenericFakeChatModel

if TYPE_CHECKING:
    import pytest

_NON_BEDROCK_MODEL = "claude-sonnet-4-20250514"


class _FakeBedrockChatModel(GenericFakeChatModel):
    """A fake chat model that reports the `amazon_bedrock` provider.

    `is_bedrock_model` inspects `get_model_provider`, which reads
    `_get_ls_params`; overriding it here is enough to make the model read as a
    Bedrock target without pulling in `langchain-aws`.
    """

    def _get_ls_params(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ls_provider": "amazon_bedrock"}


class _SentinelBedrockCaching(AgentMiddleware):
    """Stand-in for `BedrockPromptCachingMiddleware` (langchain-aws is optional)."""


def _fake_bedrock_model() -> _FakeBedrockChatModel:
    return _FakeBedrockChatModel(messages=iter([AIMessage(content="ok")]))


def _capture_stacks(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[list[str], dict[str, list[str]]]:
    """Build an agent and capture the main and per-subagent middleware class names.

    Returns a tuple of `(main_names, subagent_name -> middleware_class_names)`.
    The main stack is snapshotted via the `_validate_middleware_ordering` hook;
    subagent stacks are captured from the `SubAgentMiddleware(subagents=...)`
    call so the general-purpose and explicit subagents can be inspected too.
    """
    main_names: list[str] = []
    subagent_stacks: dict[str, list[str]] = {}

    original_validate = graph_module._validate_middleware_ordering

    def _spy_validate(middleware_list: list[Any]) -> None:
        main_names[:] = [type(m).__name__ for m in middleware_list]
        return original_validate(middleware_list)

    original_subagents = graph_module.SubAgentMiddleware

    def _spy_subagents(*args: Any, **kw: Any) -> Any:
        for spec in kw.get("subagents", []):
            if isinstance(spec, dict) and "middleware" in spec:
                subagent_stacks[spec["name"]] = [type(m).__name__ for m in spec["middleware"]]
        return original_subagents(*args, **kw)

    monkeypatch.setattr(graph_module, "_validate_middleware_ordering", _spy_validate)
    monkeypatch.setattr(graph_module, "SubAgentMiddleware", _spy_subagents)

    create_agent(**kwargs)
    return main_names, subagent_stacks


class TestBedrockPromptCachingWiring:
    def test_bedrock_model_appends_bedrock_caching_after_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Bedrock main + GP stack gets the Bedrock entry immediately after Anthropic."""
        monkeypatch.setattr(graph_module, "_create_bedrock_prompt_caching_middleware", _SentinelBedrockCaching)

        main_names, subagent_stacks = _capture_stacks(monkeypatch, model=_fake_bedrock_model())

        # Main agent: Bedrock caching is the innermost tail, right after Anthropic.
        assert main_names[-2:] == ["AnthropicPromptCachingMiddleware", "_SentinelBedrockCaching"], main_names
        # General-purpose subagent gets the same treatment.
        gp_names = subagent_stacks["general-purpose"]
        assert gp_names[-2:] == ["AnthropicPromptCachingMiddleware", "_SentinelBedrockCaching"], gp_names

    def test_bedrock_explicit_subagent_gets_bedrock_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit subagent on a Bedrock model gets Bedrock caching even when the main model is not Bedrock."""
        monkeypatch.setattr(graph_module, "_create_bedrock_prompt_caching_middleware", _SentinelBedrockCaching)

        main_names, subagent_stacks = _capture_stacks(
            monkeypatch,
            model=_NON_BEDROCK_MODEL,
            subagents=[
                {
                    "name": "bedrock-worker",
                    "description": "Uses Bedrock.",
                    "system_prompt": "Help with Bedrock tasks.",
                    "model": _fake_bedrock_model(),
                }
            ],
        )

        # The non-Bedrock main agent does NOT get the Bedrock entry...
        assert "_SentinelBedrockCaching" not in main_names, main_names
        assert main_names[-1] == "AnthropicPromptCachingMiddleware", main_names
        # ...but the Bedrock subagent does.
        worker_names = subagent_stacks["bedrock-worker"]
        assert worker_names[-2:] == ["AnthropicPromptCachingMiddleware", "_SentinelBedrockCaching"], worker_names

    def test_non_bedrock_stack_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-Bedrock model never gets a Bedrock entry and never consults the helper.

        This is the byte-identical guarantee: for a non-Bedrock model the helper
        is not even called, so the class-name list is exactly what it was before
        Bedrock caching existed (Anthropic caching strictly last).
        """
        call_count = {"n": 0}

        def _tracking_helper() -> AgentMiddleware[Any, Any, Any] | None:
            call_count["n"] += 1
            return _SentinelBedrockCaching()

        monkeypatch.setattr(graph_module, "_create_bedrock_prompt_caching_middleware", _tracking_helper)

        main_names, subagent_stacks = _capture_stacks(monkeypatch, model=_NON_BEDROCK_MODEL)

        assert call_count["n"] == 0, "Bedrock helper must not be called for a non-Bedrock model"
        assert "_SentinelBedrockCaching" not in main_names, main_names
        assert main_names[-1] == "AnthropicPromptCachingMiddleware", main_names
        gp_names = subagent_stacks["general-purpose"]
        assert "_SentinelBedrockCaching" not in gp_names, gp_names
        assert gp_names[-1] == "AnthropicPromptCachingMiddleware", gp_names

    def test_bedrock_caching_optional_when_langchain_aws_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Bedrock model builds fine when langchain-aws is missing — no crash, no Bedrock entry.

        The real `_create_bedrock_prompt_caching_middleware` runs (langchain-aws
        is not installed in the test env), returns `None`, and the tail falls
        back to Anthropic caching only.
        """
        main_names, subagent_stacks = _capture_stacks(monkeypatch, model=_fake_bedrock_model())

        assert main_names[-1] == "AnthropicPromptCachingMiddleware", main_names
        assert main_names.count("AnthropicPromptCachingMiddleware") == 1, main_names
        gp_names = subagent_stacks["general-purpose"]
        assert gp_names[-1] == "AnthropicPromptCachingMiddleware", gp_names


class TestCreateBedrockPromptCachingMiddleware:
    def test_returns_none_when_langchain_aws_unavailable(self) -> None:
        """With langchain-aws not installed, the helper degrades to `None`."""
        assert graph_module._create_bedrock_prompt_caching_middleware() is None

    def test_returns_middleware_when_langchain_aws_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the module imports, the helper instantiates its middleware class with ignore behavior."""
        import types

        captured_kwargs: dict[str, Any] = {}

        class _StubBedrockCaching:
            def __init__(self, **kwargs: Any) -> None:
                captured_kwargs.update(kwargs)

        fake_module = types.ModuleType("langchain_aws.middleware.prompt_caching")
        fake_module.BedrockPromptCachingMiddleware = _StubBedrockCaching  # type: ignore[attr-defined]
        monkeypatch.setattr(graph_module, "import_module", lambda name: fake_module)

        result = graph_module._create_bedrock_prompt_caching_middleware()

        assert isinstance(result, _StubBedrockCaching)
        assert captured_kwargs == {"unsupported_model_behavior": "ignore"}

    def test_preserves_unrelated_import_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ImportError naming an unrelated transitive dependency is re-raised, not swallowed."""
        import pytest

        def _raise(name: str) -> Any:
            raise ImportError(name="some_unrelated_dep")

        monkeypatch.setattr(graph_module, "import_module", _raise)
        with pytest.raises(ImportError):
            graph_module._create_bedrock_prompt_caching_middleware()


class TestAppendPromptCachingMiddleware:
    def test_non_bedrock_appends_only_anthropic(self) -> None:
        """For a non-Bedrock model spec only the Anthropic entry is appended."""
        stack: list[AgentMiddleware[Any, Any, Any]] = []
        graph_module._append_prompt_caching_middleware(stack, _NON_BEDROCK_MODEL)
        assert [type(m).__name__ for m in stack] == ["AnthropicPromptCachingMiddleware"]

    def test_bedrock_appends_anthropic_then_bedrock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """For a Bedrock model the Anthropic entry precedes the Bedrock entry."""
        monkeypatch.setattr(graph_module, "_create_bedrock_prompt_caching_middleware", _SentinelBedrockCaching)
        stack: list[AgentMiddleware[Any, Any, Any]] = []
        graph_module._append_prompt_caching_middleware(stack, _fake_bedrock_model())
        assert [type(m).__name__ for m in stack] == ["AnthropicPromptCachingMiddleware", "_SentinelBedrockCaching"]

    def test_bedrock_appends_only_anthropic_when_helper_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Bedrock model with langchain-aws absent still yields only the Anthropic entry."""
        monkeypatch.setattr(graph_module, "_create_bedrock_prompt_caching_middleware", lambda: None)
        stack: list[AgentMiddleware[Any, Any, Any]] = []
        graph_module._append_prompt_caching_middleware(stack, _fake_bedrock_model())
        assert [type(m).__name__ for m in stack] == ["AnthropicPromptCachingMiddleware"]
