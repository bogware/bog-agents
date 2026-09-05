"""Vendor-neutral OTLP export of GenAI-semconv spans (ROADMAP #74).

`OTelExportMiddleware` turns every model call, tool call and subagent spawn
into an OpenTelemetry span carrying the GenAI semantic-convention attributes
(`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, …) plus bog's cost attribute (`bog.cost_usd`).
Spans go to an injected `SpanSink`; `OTLPHttpSink` posts them as OTLP/HTTP
JSON to any collector (`/v1/traces`) with nothing but the standard library, so
no OpenTelemetry SDK is required — and when one *is* installed with a
LangSmith exporter, that is simply another sink. `BufferSink` captures spans
for tests and for the CLI's `/trace` views.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from langchain.agents.middleware.types import ModelRequest
    from langchain.tools.tool_node import ToolCallRequest

logger = logging.getLogger(__name__)

GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
BOG_COST_USD = "bog.cost_usd"
BOG_MIDDLEWARE = "bog.middleware"


@dataclass
class GenAISpan:
    """One span in OTLP terms (nanosecond timestamps, flat attributes)."""

    name: str
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str = ""
    status_error: str = ""

    def to_otlp(self) -> dict[str, Any]:
        """The OTLP/JSON span object."""
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "kind": 1,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.end_ns),
            "attributes": [_attribute(k, v) for k, v in self.attributes.items() if v is not None],
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        span["status"] = {"code": 2, "message": self.status_error} if self.status_error else {"code": 1}
        return span


def _attribute(key: str, value: Any) -> dict[str, Any]:  # noqa: ANN401 - OTLP any-value
    if isinstance(value, bool):
        typed: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        typed = {"intValue": str(value)}
    elif isinstance(value, float):
        typed = {"doubleValue": value}
    else:
        typed = {"stringValue": str(value)}
    return {"key": key, "value": typed}


def otlp_traces_payload(spans: Sequence[GenAISpan], *, service_name: str, service_version: str = "") -> dict[str, Any]:
    """The `POST /v1/traces` body for these spans."""
    resource_attrs = [_attribute("service.name", service_name)]
    if service_version:
        resource_attrs.append(_attribute("service.version", service_version))
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource_attrs},
                "scopeSpans": [{"scope": {"name": "bog_agents.otel_export"}, "spans": [s.to_otlp() for s in spans]}],
            }
        ]
    }


class SpanSink(Protocol):
    """Where finished spans go."""

    def export(self, spans: Sequence[GenAISpan]) -> None:
        """Deliver spans (must not raise into the agent)."""


class BufferSink:
    """Keeps spans in memory (tests, `/trace`)."""

    def __init__(self) -> None:
        """Start empty."""
        self.spans: list[GenAISpan] = []

    def export(self, spans: Sequence[GenAISpan]) -> None:
        """Append."""
        self.spans.extend(spans)


class OTLPHttpSink:
    """Posts OTLP/HTTP JSON to a collector; failures are logged, never raised."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        service_name: str = "bog-agents",
        service_version: str = "",
        timeout: float = 5.0,
        post: Callable[[str, bytes, dict[str, str]], None] | None = None,
    ) -> None:
        """`endpoint` is the collector base URL or the full `/v1/traces` URL."""
        self.endpoint = endpoint if endpoint.rstrip("/").endswith("/v1/traces") else endpoint.rstrip("/") + "/v1/traces"
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.service_name = service_name
        self.service_version = service_version
        self._timeout = timeout
        self._post = post or self._urllib_post

    def _urllib_post(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - operator-configured collector URL
        with urllib.request.urlopen(request, timeout=self._timeout):  # noqa: S310 - operator-configured collector URL
            pass

    def export(self, spans: Sequence[GenAISpan]) -> None:
        """Send one batch."""
        if not spans:
            return
        body = json.dumps(otlp_traces_payload(spans, service_name=self.service_name, service_version=self.service_version)).encode("utf-8")
        try:
            self._post(self.endpoint, body, self.headers)
        except Exception:  # noqa: BLE001 - telemetry must never break the agent
            logger.warning("OTLP export to %s failed", self.endpoint, exc_info=True)


def _usage(response: ModelResponse) -> tuple[str, int, int]:
    for message in reversed(getattr(response, "result", None) or []):
        if getattr(message, "type", "") != "ai":
            continue
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        return str(meta.get("model_name") or meta.get("model") or ""), int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    return "", 0, 0


class OTelExportMiddleware(AgentMiddleware[Any, Any, Any]):
    """Emit GenAI-semconv spans for model calls, tool calls and subagent spawns."""

    def __init__(
        self,
        sink: SpanSink,
        *,
        system: str = "",
        agent_name: str = "agent",
        price: Callable[[str, int, int], float | None] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        """Bind to a sink; `price(model, in, out)` adds `bog.cost_usd` to model spans."""
        super().__init__()
        self._sink = sink
        self._system = system
        self._agent_name = agent_name
        self._price = price
        self._clock_ns = clock_ns
        self.trace_id = uuid.uuid4().hex

    def _emit(self, span: GenAISpan) -> None:
        try:
            self._sink.export([span])
        except Exception:  # noqa: BLE001 - telemetry must never break the agent
            logger.debug("span sink failed", exc_info=True)

    def _model_span(self, request: ModelRequest, response: ModelResponse | None, start_ns: int, error: str = "") -> None:
        model, tokens_in, tokens_out = _usage(response) if response is not None else ("", 0, 0)
        if not model:
            model = str(getattr(getattr(request, "model", None), "model_name", "") or getattr(getattr(request, "model", None), "model", "") or "")
        attrs: dict[str, Any] = {
            GEN_AI_OPERATION: "chat",
            GEN_AI_SYSTEM: self._system or model.split(":", 1)[0] if ":" in model else self._system,
            GEN_AI_REQUEST_MODEL: model,
            GEN_AI_AGENT_NAME: self._agent_name,
            GEN_AI_INPUT_TOKENS: tokens_in,
            GEN_AI_OUTPUT_TOKENS: tokens_out,
        }
        if self._price and (tokens_in or tokens_out):
            attrs[BOG_COST_USD] = self._price(model, tokens_in, tokens_out)
        self._emit(
            GenAISpan(
                name=f"chat {model or 'model'}",
                start_ns=start_ns,
                end_ns=self._clock_ns(),
                attributes=attrs,
                trace_id=self.trace_id,
                status_error=error,
            )
        )

    def wrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:  # type: ignore[override]  # noqa: ANN401
        """Span around the model call."""
        start = self._clock_ns()
        try:
            response = handler(request)
        except Exception as exc:
            self._model_span(request, None, start, error=exc.__class__.__name__)
            raise
        self._model_span(request, response, start)
        return response

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> ModelResponse:  # type: ignore[override]  # noqa: ANN401
        """Async twin of `wrap_model_call`."""
        start = self._clock_ns()
        try:
            response = await handler(request)
        except Exception as exc:
            self._model_span(request, None, start, error=exc.__class__.__name__)
            raise
        self._model_span(request, response, start)
        return response

    def _tool_span(self, request: ToolCallRequest, start_ns: int, error: str = "") -> None:
        call = getattr(request, "tool_call", None) or {}
        name = str(call.get("name", ""))
        operation = "invoke_agent" if name == "task" else "execute_tool"
        attrs: dict[str, Any] = {GEN_AI_OPERATION: operation, GEN_AI_TOOL_NAME: name, GEN_AI_AGENT_NAME: self._agent_name}
        if name == "task":
            attrs["gen_ai.agent.subagent"] = str((call.get("args") or {}).get("subagent_type", ""))
        self._emit(
            GenAISpan(
                name=f"{operation} {name}", start_ns=start_ns, end_ns=self._clock_ns(), attributes=attrs, trace_id=self.trace_id, status_error=error
            )
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:  # type: ignore[override]  # noqa: ANN401
        """Span around the tool call (`invoke_agent` for `task`)."""
        start = self._clock_ns()
        try:
            result = handler(request)
        except Exception as exc:
            self._tool_span(request, start, error=exc.__class__.__name__)
            raise
        self._tool_span(request, start)
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:  # type: ignore[override]  # noqa: ANN401
        """Async twin of `wrap_tool_call`."""
        start = self._clock_ns()
        try:
            result = await handler(request)
        except Exception as exc:
            self._tool_span(request, start, error=exc.__class__.__name__)
            raise
        self._tool_span(request, start)
        return result


__all__ = [
    "BOG_COST_USD",
    "GEN_AI_INPUT_TOKENS",
    "GEN_AI_OPERATION",
    "GEN_AI_OUTPUT_TOKENS",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_SYSTEM",
    "GEN_AI_TOOL_NAME",
    "BufferSink",
    "GenAISpan",
    "OTLPHttpSink",
    "OTelExportMiddleware",
    "SpanSink",
    "otlp_traces_payload",
]
