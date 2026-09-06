"""ROADMAP #74: GenAI-semconv spans and the dependency-free OTLP/HTTP sink."""

from __future__ import annotations

import json

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from bog_agents import otel_export as ox


class _Req:
    model = None


def test_middleware_emits_semconv_spans() -> None:
    sink = ox.BufferSink()
    clock = iter(range(1_000, 1_100))
    mw = ox.OTelExportMiddleware(sink, system="anthropic", agent_name="main", price=lambda m, i, o: 0.5, clock_ns=lambda: next(clock))
    reply = AIMessage(
        content="ok", usage_metadata={"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}, response_metadata={"model_name": "claude-x"}
    )
    mw.wrap_model_call(_Req(), lambda _r: ModelResponse(result=[reply]))
    request = ToolCallRequest(
        tool_call={"name": "task", "args": {"subagent_type": "scout", "description": "x"}, "id": "c1"}, tool=None, state={}, runtime=None
    )  # type: ignore[arg-type]
    mw.wrap_tool_call(request, lambda _r: ToolMessage(content="done", tool_call_id="c1", name="task"))

    def boom(_r: ToolCallRequest) -> ToolMessage:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        mw.wrap_tool_call(ToolCallRequest(tool_call={"name": "execute", "args": {}, "id": "c2"}, tool=None, state={}, runtime=None), boom)  # type: ignore[arg-type]

    names = [s.name for s in sink.spans]
    assert names == ["chat claude-x", "invoke_agent task", "execute_tool execute"]
    model_span = sink.spans[0]
    assert model_span.attributes[ox.GEN_AI_OPERATION] == "chat"
    assert model_span.attributes[ox.GEN_AI_REQUEST_MODEL] == "claude-x"
    assert model_span.attributes[ox.GEN_AI_SYSTEM] == "anthropic"
    assert model_span.attributes[ox.GEN_AI_INPUT_TOKENS] == 30 and model_span.attributes[ox.GEN_AI_OUTPUT_TOKENS] == 5
    assert model_span.attributes[ox.BOG_COST_USD] == 0.5
    assert model_span.end_ns > model_span.start_ns
    assert sink.spans[1].attributes["gen_ai.agent.subagent"] == "scout"
    assert sink.spans[2].status_error == "ValueError"
    assert len({s.trace_id for s in sink.spans}) == 1


def test_otlp_payload_shape_and_http_sink() -> None:
    span = ox.GenAISpan(
        name="chat m", start_ns=1, end_ns=2, attributes={ox.GEN_AI_INPUT_TOKENS: 3, ox.BOG_COST_USD: 0.25, "flag": True, "model": "m"}
    )
    payload = ox.otlp_traces_payload([span], service_name="svc", service_version="1.0")
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0]["name"] == "chat m" and spans[0]["status"] == {"code": 1}
    attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
    assert attrs[ox.GEN_AI_INPUT_TOKENS] == {"intValue": "3"} and attrs[ox.BOG_COST_USD] == {"doubleValue": 0.25}
    assert attrs["flag"] == {"boolValue": True} and attrs["model"] == {"stringValue": "m"}
    resource = {a["key"]: a["value"]["stringValue"] for a in payload["resourceSpans"][0]["resource"]["attributes"]}
    assert resource == {"service.name": "svc", "service.version": "1.0"}

    posted: list[tuple[str, dict, dict]] = []
    sink = ox.OTLPHttpSink(
        "http://collector:4318",
        headers={"Authorization": "Bearer x"},
        post=lambda url, body, headers: posted.append((url, json.loads(body), headers)),
    )
    sink.export([span])
    sink.export([])
    assert len(posted) == 1
    url, body, headers = posted[0]
    assert url == "http://collector:4318/v1/traces" and headers["Authorization"] == "Bearer x"
    assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "chat m"

    def failing(url: str, body: bytes, headers: dict) -> None:
        raise OSError("down")

    ox.OTLPHttpSink("http://collector:4318/v1/traces", post=failing).export([span])  # logged, not raised
