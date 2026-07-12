"""Tests for the multi-format tool-call parser middleware."""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from bog_agents.middleware.tool_call_parser import (
    ToolCallParserMiddleware,
    _balanced_json_objects,
    _coerce_call,
    parse_tool_calls_from_text,
)

# ---------------------------------------------------------------------------
# Format-specific parser tests
# ---------------------------------------------------------------------------


class TestMistralFormat:
    """[TOOL_CALLS]{...} as emitted by mistral-nemo, mixtral, mistral-small."""

    def test_single_object(self) -> None:
        text = '[TOOL_CALLS]{"name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}'
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "write_file"
        assert calls[0]["args"]["path"] == "a.txt"
        assert residual == ""

    def test_array_of_objects(self) -> None:
        text = '[TOOL_CALLS][{"name": "read_file", "arguments": {"path": "a"}}, {"name": "write_file", "arguments": {"path": "b", "content": "x"}}]'
        calls, _ = parse_tool_calls_from_text(text)
        assert [c["name"] for c in calls] == ["read_file", "write_file"]

    def test_with_explicit_close_tag(self) -> None:
        text = '[TOOL_CALLS]{"name": "ls", "arguments": {"path": "/"}}[/TOOL_CALLS]'
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "ls"
        assert residual == ""

    def test_preserves_surrounding_text(self) -> None:
        text = 'Sure, I\'ll do that.\n[TOOL_CALLS]{"name": "write_file", "arguments": {"path": "x", "content": "y"}}\nLet me know if anything else.'
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert "Sure, I'll do that." in residual
        assert "Let me know if anything else." in residual

    def test_stringified_arguments(self) -> None:
        # Some Mistral fine-tunes encode `arguments` as a JSON string.
        text = '[TOOL_CALLS]{"name": "write_file", "arguments": "{\\"path\\": \\"a\\", \\"content\\": \\"b\\"}"}'
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["args"]["path"] == "a"
        assert calls[0]["args"]["content"] == "b"


class TestHermesFormat:
    """<tool_call>{...}</tool_call> XML-tagged calls."""

    def test_single_call(self) -> None:
        text = '<tool_call>{"name": "search", "arguments": {"q": "weather"}}</tool_call>'
        calls, residual = parse_tool_calls_from_text(text)
        assert calls[0]["name"] == "search"
        assert calls[0]["args"]["q"] == "weather"
        assert residual == ""

    def test_multiline_body(self) -> None:
        text = '<tool_call>\n{"name": "write_file",\n "arguments": {"path": "a.ts", "content": "export const x = 1;"}}\n</tool_call>'
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["name"] == "write_file"

    def test_multiple_calls_in_one_message(self) -> None:
        text = '<tool_call>{"name": "a", "arguments": {}}</tool_call> some prose <tool_call>{"name": "b", "arguments": {}}</tool_call>'
        calls, residual = parse_tool_calls_from_text(text)
        assert [c["name"] for c in calls] == ["a", "b"]
        assert "some prose" in residual

    def test_anthropic_style_input_key(self) -> None:
        # Some templates spell args as "input" (Anthropic XML carryover).
        text = '<tool_call>{"name": "ls", "input": {"path": "/tmp"}}</tool_call>'
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["args"]["path"] == "/tmp"


class TestFencedJSON:
    """Fenced code blocks tagged json/tool_call/function."""

    def test_tool_call_fence(self) -> None:
        text = 'Calling the tool now.\n```tool_call\n{"name": "write_file", "arguments": {"path": "a", "content": "b"}}\n```\nDone.'
        calls, residual = parse_tool_calls_from_text(text)
        assert calls[0]["name"] == "write_file"
        assert "Calling the tool now." in residual
        assert "Done." in residual

    def test_function_fence(self) -> None:
        text = '```function\n{"name": "search", "arguments": {"q": "x"}}\n```'
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["name"] == "search"


class TestFunctionXMLFormat:
    """Nemotron / Llama `<function=NAME>...</function>` XML tool calls."""

    def test_function_attribute_form(self) -> None:
        text = "Run this.\n<function=grep><parameter name=pattern>MAGIC</parameter><parameter name=path>/workspace/tmp</parameter></function>"
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "grep"
        assert calls[0]["args"] == {"pattern": "MAGIC", "path": "/workspace/tmp"}
        assert residual == "Run this."

    def test_multiple_function_blocks(self) -> None:
        text = (
            "<function=ls><parameter name=path>/</parameter></function> then "
            "<function=read_file><parameter name=file_path>/a.txt</parameter></function>"
        )
        calls, residual = parse_tool_calls_from_text(text)
        assert [c["name"] for c in calls] == ["ls", "read_file"]
        assert "then" in residual

    def test_alternate_name_child_form(self) -> None:
        # <function><name=NAME</name>...<parameter>...</parameter></function> plus a
        # trailing </tool_call> sentinel, with an inline `<key>:value` argument body.
        text = "<function>\n<name=get_service_name</name>\n<parameter>\n<service_id>:0\n</parameter>\n</function>\n</tool_call>"
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "get_service_name"
        assert calls[0]["args"] == {"service_id": "0"}
        assert "</tool_call>" not in residual

    def test_alternate_named_parameter_form(self) -> None:
        text = "<function><name>write_file</name><parameter name=file_path>/x.txt</parameter><parameter name=content>hi</parameter></function>"
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["name"] == "write_file"
        assert calls[0]["args"] == {"file_path": "/x.txt", "content": "hi"}

    def test_format_filter_excludes_function(self) -> None:
        text = "<function=grep><parameter name=pattern>x</parameter></function>"
        calls, _ = parse_tool_calls_from_text(text, formats=("mistral", "hermes"))
        assert calls == []


class TestBareJSON:
    """Whole-message bare JSON objects that are tool-call-shaped."""

    def test_tool_args_shape(self) -> None:
        calls, residual = parse_tool_calls_from_text('{"tool": "search", "args": {"q": "x"}}')
        assert calls[0]["name"] == "search"
        assert calls[0]["args"] == {"q": "x"}
        assert residual == ""

    def test_name_arguments_shape(self) -> None:
        calls, _ = parse_tool_calls_from_text('{"name": "ls", "arguments": {"path": "/"}}')
        assert calls[0]["name"] == "ls"
        assert calls[0]["args"] == {"path": "/"}

    def test_shell_style_command_key(self) -> None:
        # Nemotron sometimes emits a bare shell call with `cmd`/`command`.
        calls, _ = parse_tool_calls_from_text('{"tool": "run", "cmd": "pytest -q"}')
        assert calls[0]["name"] == "run"
        assert calls[0]["args"] == {"command": "pytest -q"}


# ---------------------------------------------------------------------------
# Pass-through behaviour
# ---------------------------------------------------------------------------


class TestGemmaFormat:
    """Gemma 4 native `<|tool_call>call:NAME{...}<tool_call|>` format."""

    def test_single_call_with_string_args(self) -> None:
        text = '<|tool_call>call:write_file{file_path:<|"|>ack.txt<|"|>,content:<|"|>ACK<|"|>}<tool_call|>'
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "write_file"
        assert calls[0]["args"] == {"file_path": "ack.txt", "content": "ACK"}
        assert residual == ""

    def test_numeric_and_bool_args(self) -> None:
        text = "<|tool_call>call:set_temp{value:42,active:true}<tool_call|>"
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["args"] == {"value": 42, "active": True}

    def test_string_with_special_chars(self) -> None:
        text = '<|tool_call>call:run{cmd:<|"|>echo "hi", world<|"|>}<tool_call|>'
        calls, _ = parse_tool_calls_from_text(text)
        assert calls[0]["args"] == {"cmd": 'echo "hi", world'}

    def test_preserves_surrounding_prose(self) -> None:
        text = 'I\'ll create the file now.\n<|tool_call>call:write_file{file_path:<|"|>a<|"|>,content:<|"|>b<|"|>}<tool_call|>\nDone.'
        calls, residual = parse_tool_calls_from_text(text)
        assert len(calls) == 1
        assert "create the file now" in residual
        assert "Done." in residual

    def test_format_filter_excludes_gemma(self) -> None:
        text = '<|tool_call>call:f{x:<|"|>1<|"|>}<tool_call|>'
        calls, _ = parse_tool_calls_from_text(text, formats=("mistral", "hermes"))
        assert calls == []


class TestThinkingStrip:
    """Reasoning-mode envelopes are stripped before parsing."""

    def test_call_after_think_block(self) -> None:
        text = (
            "<think>I should use write_file to create the file.</think>\n"
            '[TOOL_CALLS]{"name": "write_file", "arguments": {"path": "a", "content": "b"}}'
        )
        calls, residual = parse_tool_calls_from_text(text)
        assert calls[0]["name"] == "write_file"
        assert "<think>" not in residual

    def test_thinking_disabled_keeps_envelope(self) -> None:
        text = '<think>reasoning</think>[TOOL_CALLS]{"name": "f", "arguments": {}}'
        calls, residual = parse_tool_calls_from_text(text, strip_thinking=False)
        # Call still parses (it's outside the think block) but envelope survives.
        assert len(calls) == 1
        assert "<think>" in residual or "reasoning" in residual

    def test_call_inside_think_recovered_when_stripped_disabled(self) -> None:
        # Edge: tool call accidentally emitted inside <think> by a confused model.
        # With strip_thinking=False, the parser still sees and recovers it
        # because the regex doesn't care about envelope context.
        text = '<think>I will call [TOOL_CALLS]{"name": "f", "arguments": {}} now.</think>'
        calls, _ = parse_tool_calls_from_text(text, strip_thinking=False)
        assert len(calls) == 1


class TestPassThrough:
    """Plain text with no tool calls should be untouched."""

    def test_plain_text(self) -> None:
        text = "Hello, this is just a regular response with no tool calls."
        calls, residual = parse_tool_calls_from_text(text)
        assert calls == []
        assert residual == text.strip()

    def test_empty_string(self) -> None:
        calls, residual = parse_tool_calls_from_text("")
        assert calls == []
        assert residual == ""

    def test_json_in_prose_not_a_tool_call(self) -> None:
        # A JSON example inside narrative text shouldn't trigger.
        text = 'Here is an example: `{"key": "value"}`. End of message.'
        calls, _ = parse_tool_calls_from_text(text)
        assert calls == []

    def test_malformed_mistral_body_is_skipped(self) -> None:
        text = "[TOOL_CALLS]{not actually json}"
        calls, _ = parse_tool_calls_from_text(text)
        assert calls == []


# ---------------------------------------------------------------------------
# Coerce / normalisation
# ---------------------------------------------------------------------------


class TestCoerce:
    def test_openai_style(self) -> None:
        out = _coerce_call({"name": "f", "arguments": {"x": 1}})
        assert out == {"name": "f", "args": {"x": 1}}

    def test_anthropic_style(self) -> None:
        out = _coerce_call({"name": "f", "input": {"x": 1}})
        assert out == {"name": "f", "args": {"x": 1}}

    def test_wrapped_function(self) -> None:
        out = _coerce_call({"type": "function", "function": {"name": "f", "arguments": {"x": 1}}})
        assert out == {"name": "f", "args": {"x": 1}}

    def test_tool_alias(self) -> None:
        out = _coerce_call({"tool": "f", "args": {"x": 1}})
        assert out == {"name": "f", "args": {"x": 1}}

    def test_missing_name_returns_none(self) -> None:
        assert _coerce_call({"arguments": {"x": 1}}) is None

    def test_non_dict_returns_none(self) -> None:
        assert _coerce_call(["name", "f"]) is None

    def test_string_arguments_decoded_once(self) -> None:
        out = _coerce_call({"name": "f", "arguments": '{"x": 1}'})
        assert out == {"name": "f", "args": {"x": 1}}

    def test_string_arguments_undecodable_wrapped(self) -> None:
        out = _coerce_call({"name": "f", "arguments": "raw text"})
        assert out == {"name": "f", "args": {"value": "raw text"}}


class TestBalancedScanner:
    def test_two_back_to_back_objects(self) -> None:
        text = '{"a": 1}{"b": 2}'
        assert _balanced_json_objects(text) == ['{"a": 1}', '{"b": 2}']

    def test_nested_braces_in_strings_dont_confuse_scanner(self) -> None:
        text = '{"path": "a{b}c"}{"x": 2}'
        out = _balanced_json_objects(text)
        assert out == ['{"path": "a{b}c"}', '{"x": 2}']

    def test_handles_escaped_quotes(self) -> None:
        text = '{"text": "she said \\"hi\\""}'
        assert _balanced_json_objects(text) == [text]


# ---------------------------------------------------------------------------
# Middleware integration
# ---------------------------------------------------------------------------


class TestMiddleware:
    """End-to-end through the AgentMiddleware contract."""

    @staticmethod
    def _model_response_with(content: str, *, tool_calls: list[dict] | None = None) -> ModelResponse:
        msg = AIMessage(content=content, tool_calls=tool_calls or [])
        return ModelResponse(result=[msg], structured_response=None)

    def test_recovers_mistral_format(self) -> None:
        mw = ToolCallParserMiddleware()
        response = self._model_response_with('[TOOL_CALLS]{"name": "write_file", "arguments": {"path": "a", "content": "hi"}}')

        def call_next(_request: object) -> ModelResponse:
            return response

        result = mw.wrap_model_call(request=None, call_next=call_next)  # type: ignore[arg-type]
        assert len(result.result) == 1
        recovered = result.result[0]
        assert isinstance(recovered, AIMessage)
        assert len(recovered.tool_calls) == 1
        assert recovered.tool_calls[0]["name"] == "write_file"
        assert recovered.tool_calls[0]["args"] == {"path": "a", "content": "hi"}
        assert recovered.tool_calls[0]["id"].startswith("call_")

    def test_passthrough_when_already_structured(self) -> None:
        mw = ToolCallParserMiddleware()
        existing = [
            {"id": "abc", "name": "f", "args": {}, "type": "tool_call"},
        ]
        response = self._model_response_with("ignored body", tool_calls=existing)

        def call_next(_request: object) -> ModelResponse:
            return response

        result = mw.wrap_model_call(request=None, call_next=call_next)  # type: ignore[arg-type]
        # Original message returned unchanged.
        assert result.result[0].tool_calls == existing
        assert result.result[0].content == "ignored body"

    def test_passthrough_for_plain_text(self) -> None:
        mw = ToolCallParserMiddleware()
        response = self._model_response_with("Just a plain reply, no tool call here.")

        def call_next(_request: object) -> ModelResponse:
            return response

        result = mw.wrap_model_call(request=None, call_next=call_next)  # type: ignore[arg-type]
        assert result.result[0].tool_calls == []

    def test_non_aimessage_unchanged(self) -> None:
        mw = ToolCallParserMiddleware()
        msg = HumanMessage(content="user input")
        response = ModelResponse(result=[msg], structured_response=None)

        def call_next(_request: object) -> ModelResponse:
            return response

        result = mw.wrap_model_call(request=None, call_next=call_next)  # type: ignore[arg-type]
        assert result.result[0] is msg

    @pytest.mark.asyncio
    async def test_async_recovery(self) -> None:
        mw = ToolCallParserMiddleware()
        response = self._model_response_with('<tool_call>{"name": "ls", "arguments": {"path": "/"}}</tool_call>')

        async def call_next(_request: object) -> ModelResponse:
            return response

        result = await mw.awrap_model_call(request=None, call_next=call_next)  # type: ignore[arg-type]
        assert len(result.result[0].tool_calls) == 1
        assert result.result[0].tool_calls[0]["name"] == "ls"

    def test_format_filtering(self) -> None:
        # Restrict to mistral; hermes should be ignored.
        mw = ToolCallParserMiddleware(formats=("mistral",))
        response = self._model_response_with('<tool_call>{"name": "f", "arguments": {}}</tool_call>')

        def call_next(_request: object) -> ModelResponse:
            return response

        result = mw.wrap_model_call(request=None, call_next=call_next)  # type: ignore[arg-type]
        assert result.result[0].tool_calls == []
