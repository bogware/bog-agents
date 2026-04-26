"""Tool-call parser middleware — recover tool calls emitted as text.

Many open-weight chat models do not emit OpenAI-style structured `tool_calls`
when wrapped through Ollama's chat-completion adapter. Instead they emit the
call inside the assistant message text using their training-time format:

- **Mistral family** (mistral-nemo, mixtral, mistral-small): ``[TOOL_CALLS]{...}``
  with either a single dict or a list of dicts.
- **Hermes / NousResearch** (hermes3, hermes2): ``<tool_call>{...}</tool_call>``
  XML-ish tags, one per call.
- **Qwen2.5+ chat templates**: ``<tool_call>{...}</tool_call>`` (same as Hermes,
  but with newlines and slightly different argument keys).
- **Generic fenced JSON**: code blocks tagged ``json``/``tool_call`` containing
  ``{"name": ..., "arguments": ...}``.

The translation layer between Ollama and langchain-ollama silently drops these,
so langchain receives an `AIMessage` with empty `tool_calls`. The agent then
treats it as a final answer and stops, even though the model meant to call a
tool.

This middleware sits **after** the model call. If the response contains an
`AIMessage` with non-empty text content but empty `tool_calls`, it tries each
parser in order. When a parser matches, it converts the text into proper
`tool_calls` and clears the matched portion from the content so the next
middleware sees a well-formed structured tool call.

Drop the middleware in your stack right above (outside) any tool-execution
middleware. The CLI auto-enables it whenever the model spec starts with
``ollama:``; for direct SDK use, add it explicitly::

    from bog_agents.middleware.tool_call_parser import ToolCallParserMiddleware

    agent = create_agent(
        model="ollama:mistral-nemo:12b",
        tools=[...],
        middleware=[ToolCallParserMiddleware()],
    )

The middleware is safe to leave on for OpenAI-style models too: when the
response already has structured `tool_calls`, it's a no-op pass-through.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypedDict

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


# Mistral: [TOOL_CALLS]{...} or [TOOL_CALLS][{...}, {...}]
# We anchor on the literal marker and use the brace-balanced scanner to find
# the body's true end — so trailing prose after the call survives intact.
_MISTRAL_OPEN_RE = re.compile(r"\[TOOL_CALLS\]\s*(?=[\[{])")
_MISTRAL_CLOSE_RE = re.compile(r"\s*\[/TOOL_CALLS\]")

# Hermes/Qwen2.5 chat-template: <tool_call>...</tool_call>
_HERMES_RE = re.compile(
    r"<tool_call>\s*(?P<body>\{.*?\})\s*</tool_call>",
    re.DOTALL,
)

# Generic fenced JSON tagged as tool_call / function / json (only when the
# body looks tool-call-shaped: has a name+arguments key).
_FENCED_RE = re.compile(
    r"```(?:tool_call|function|json)\s*\n(?P<body>\{.*?\})\s*\n```",
    re.DOTALL,
)

# Gemma 4 native tool-call format. Distinct from everything else: not JSON,
# uses Python-call-ish braces for args and a custom <|"|> token as the
# string-value separator.
#
# Example output from gemma4:
#   <|tool_call>call:write_file{file_path:<|"|>ack.txt<|"|>,content:<|"|>ACK<|"|>}<tool_call|>
#
# Source: https://ai.google.dev/gemma/docs/capabilities/function-calling
_GEMMA_RE = re.compile(
    r"<\|tool_call>call:(?P<name>[A-Za-z_][\w]*)\{(?P<body>.*?)\}<tool_call\|>",
    re.DOTALL,
)
# Inside the gemma body, key:value pairs are separated by commas. Values may
# either be a quoted string `<|"|>val<|"|>`, a bare numeric, or a bare bool/null.
_GEMMA_KV_RE = re.compile(
    r"""
    (?P<key>[A-Za-z_][\w]*)        # key
    \s*:\s*
    (?:
        <\|"\|>(?P<str>.*?)<\|"\|> # string: <|"|>...<|"|>
        |(?P<num>-?\d+(?:\.\d+)?)  # number
        |(?P<bool>true|false|null) # bool/null
    )
    """,
    re.VERBOSE | re.DOTALL,
)

# Thinking-mode envelope used by reasoning models (qwen3, deepseek-r1 distills,
# granite-with-thinking, etc.). The model reasons inside <think>...</think>
# before emitting its actual response. Stripping the envelope lets the parser
# find any tool call hiding in the post-think section.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _balanced_json_objects(text: str) -> list[str]:
    """Extract top-level JSON objects/arrays from `text` by brace counting.

    Mistral's `[TOOL_CALLS]` body can be a single object, an array of objects,
    or an inline-newlined list of objects. The regex captures up to the first
    closing bracket, but real bodies are often multiline. This scanner walks
    the string character-by-character to handle nested braces inside argument
    dicts safely.

    Args:
        text: Substring starting at a `{` or `[`.

    Returns:
        List of balanced JSON substrings (objects or arrays). Empty list
        if the input is malformed.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "{[":
            depth = 1
            j = i + 1
            in_str = False
            esc = False
            while j < n and depth > 0:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c in "{[":
                        depth += 1
                    elif c in "}]":
                        depth -= 1
                j += 1
            if depth == 0:
                out.append(text[i:j])
                i = j
                continue
            # Unbalanced; bail.
            break
        i += 1
    return out


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


class _ParsedCall(TypedDict):
    name: str
    args: dict[str, Any]


def _coerce_call(obj: Any) -> _ParsedCall | None:
    """Normalise one parsed object into ``{name, args}``.

    Different model families spell the keys differently:

    - OpenAI-ish: ``{"name": "...", "arguments": {...}}``
    - Anthropic-ish: ``{"name": "...", "input": {...}}``
    - Custom: ``{"tool": "...", "args": {...}}`` or ``{"function": "...", "parameters": {...}}``
    - Wrapped: ``{"function": {"name": "...", "arguments": "..."}, "type": "function"}``

    Some models also stringify ``arguments`` as a JSON-encoded string, so this
    function will attempt one round of JSON-parsing on string args bodies.

    Args:
        obj: Any object decoded from a tool-call payload.

    Returns:
        A ``_ParsedCall`` dict, or ``None`` if the object can't be normalised.
    """
    if not isinstance(obj, dict):
        return None

    # Unwrap an OpenAI-style wrapper: {"type": "function", "function": {...}}
    if "function" in obj and isinstance(obj["function"], dict) and (
        "name" in obj["function"] or "arguments" in obj["function"]
    ):
        obj = obj["function"]

    name = obj.get("name") or obj.get("tool") or obj.get("recipient")
    if not isinstance(name, str) or not name:
        return None

    raw_args = (
        obj.get("arguments")
        if "arguments" in obj
        else obj.get("args")
        if "args" in obj
        else obj.get("parameters")
        if "parameters" in obj
        else obj.get("input")
        if "input" in obj
        else {}
    )

    # arguments may be a JSON-encoded string — decode once.
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (TypeError, ValueError):
            # Some models emit a single positional arg as a bare string.
            # Wrap it under a generic key so the tool sees something.
            raw_args = {"value": raw_args}

    if not isinstance(raw_args, dict):
        # Lists or scalars at the args level — wrap.
        raw_args = {"value": raw_args}

    return {"name": name, "args": raw_args}


def _parse_body(body: str) -> list[_ParsedCall]:
    """Decode a JSON body that may be a single call or a list."""
    body = body.strip()
    if not body:
        return []
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError):
        # Fall back to brace-balancing — Mistral often emits multiple
        # back-to-back objects without a containing array.
        objs = _balanced_json_objects(body)
        decoded = []
        for o in objs:
            try:
                decoded.append(json.loads(o))
            except (TypeError, ValueError):  # noqa: PERF203
                continue
        if not decoded:
            return []

    items = decoded if isinstance(decoded, list) else [decoded]
    out: list[_ParsedCall] = []
    for item in items:
        call = _coerce_call(item)
        if call is not None:
            out.append(call)
    return out


def parse_tool_calls_from_text(
    text: str,
    *,
    formats: tuple[str, ...] = ("mistral", "hermes", "fenced", "gemma"),
    strip_thinking: bool = True,
) -> tuple[list[_ParsedCall], str]:
    """Extract tool calls from raw assistant text.

    Returns the parsed calls (possibly empty) and the residual text with the
    matched portions removed. Callers can replace the message content with
    the residual so downstream middleware sees a clean message.

    Args:
        text: Assistant message content.
        formats: Which patterns to attempt, in order.
        strip_thinking: When `True`, remove `<think>...</think>` envelopes
            before parsing. Reasoning-mode models (qwen3, deepseek-r1
            distills) bury their tool call after the thinking block; without
            stripping, surrounding-text heuristics can miss the call.

    Returns:
        A `(calls, residual_text)` pair. `calls` is empty if nothing matched.
    """
    if not text:
        return [], text
    calls: list[_ParsedCall] = []
    residual = text
    if strip_thinking and "<think>" in residual.lower():
        residual = _THINK_RE.sub("", residual)
    for fmt in formats:
        if fmt == "mistral":
            calls_part, residual = _strip_mistral(residual)
            calls.extend(calls_part)
        elif fmt == "hermes":
            calls_part, residual = _strip_pattern(residual, _HERMES_RE)
            calls.extend(calls_part)
        elif fmt == "fenced":
            calls_part, residual = _strip_pattern(residual, _FENCED_RE)
            calls.extend(calls_part)
        elif fmt == "gemma":
            calls_part, residual = _strip_gemma(residual)
            calls.extend(calls_part)
    # Last-resort: if no markers matched but the whole message body is a
    # single JSON object that looks tool-call-shaped (has a `name` key + a
    # plausible args key), accept it. This catches Hermes-style emissions
    # where the model bypasses the marker and just dumps a function-call
    # object as its entire response. Only fires when nothing else matched
    # so it doesn't false-positive on content that already had structured
    # markers parsed out.
    if not calls:
        bare = _strip_bare_object(residual)
        if bare is not None:
            parsed_call, residual = bare
            calls.append(parsed_call)
    return calls, residual.strip()


def _required_fields_of_tool(tool: Any) -> list[str]:
    """Best-effort extraction of required-field names from a BaseTool schema.

    Tries both the pydantic-v1 `args_schema` interface (langchain's BaseTool
    historic shape) and the v2 `model_fields` interface. Returns an empty
    list if nothing can be discovered, in which case `_empty_args_check`
    skips the gate to avoid false positives.
    """
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return []
    # pydantic v2: model_fields with required=True implied by ellipsis default
    model_fields = getattr(schema, "model_fields", None)
    if isinstance(model_fields, dict) and model_fields:
        out: list[str] = []
        for name, field in model_fields.items():
            is_required = getattr(field, "is_required", None)
            if callable(is_required):
                try:
                    if is_required():
                        out.append(name)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            # Fallback: a missing default + no default_factory implies required
            default = getattr(field, "default", None)
            default_factory = getattr(field, "default_factory", None)
            if default in (None, Ellipsis) and default_factory is None:
                out.append(name)
        return out
    # pydantic v1: __fields__ dict with .required attribute
    legacy_fields = getattr(schema, "__fields__", None)
    if isinstance(legacy_fields, dict):
        return [n for n, f in legacy_fields.items() if getattr(f, "required", False)]
    return []


def _strip_bare_object(text: str) -> tuple[_ParsedCall, str] | None:
    """Recover a tool call when the entire message body is one JSON object.

    Models sometimes drop the marker tags entirely and just emit
    ``{"name": "f", "arguments": {...}}`` as the whole reply. This is
    structurally indistinguishable from a tool-call marker for our purposes,
    but we only accept it when (a) the message is essentially just the
    object, ignoring whitespace/code-fences, and (b) it has a plausible
    name + args/arguments/parameters/input field.

    Returns:
        A `(call, residual)` pair if recovered, else `None`.
    """
    stripped = text.strip()
    if not stripped:
        return None
    # Tolerate a wrapping ```json fence even if our marker regex didn't
    # catch it (it requires \n separators which not all models emit).
    if stripped.startswith("```"):
        # Strip the opening fence (with optional language tag) and closing fence.
        first_nl = stripped.find("\n")
        if first_nl == -1:
            return None
        body = stripped[first_nl + 1 :]
        if body.endswith("```"):
            body = body[:-3].rstrip()
        stripped = body.strip()
    if not stripped or stripped[0] != "{" or stripped[-1] != "}":
        return None
    objects = _balanced_json_objects(stripped)
    if len(objects) != 1 or objects[0] != stripped:
        return None
    try:
        decoded = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    # Must look tool-call-shaped: a name + a plausible args key.
    has_name = isinstance(decoded.get("name") or decoded.get("tool"), str)
    has_args = any(k in decoded for k in ("arguments", "args", "parameters", "input"))
    if not (has_name and has_args):
        # Wrapped form: {"type": "function", "function": {...}}
        if (
            decoded.get("type") == "function"
            and isinstance(decoded.get("function"), dict)
        ):
            pass
        else:
            return None
    parsed = _coerce_call(decoded)
    if parsed is None:
        return None
    return parsed, ""


def _strip_pattern(text: str, pattern: re.Pattern[str]) -> tuple[list[_ParsedCall], str]:
    """Extract every match of `pattern` and return calls + residual text."""
    calls: list[_ParsedCall] = []
    parts: list[str] = []
    last_end = 0
    matched = False
    for match in pattern.finditer(text):
        parsed = _parse_body(match.group("body"))
        if not parsed:
            continue
        calls.extend(parsed)
        parts.append(text[last_end : match.start()])
        last_end = match.end()
        matched = True
    if not matched:
        return [], text
    parts.append(text[last_end:])
    return calls, "".join(parts)


def _strip_gemma(text: str) -> tuple[list[_ParsedCall], str]:
    """Special-case Gemma 4's `<|tool_call>call:NAME{...}<tool_call|>` format.

    The body uses a Python-call-ish syntax: key:value pairs separated by
    commas, with string values wrapped in the custom token `<|"|>`. Numbers
    and booleans appear bare. We extract via _GEMMA_KV_RE which captures
    the three shapes.
    """
    calls: list[_ParsedCall] = []
    parts: list[str] = []
    last_end = 0
    matched = False
    for m in _GEMMA_RE.finditer(text):
        name = m.group("name")
        body = m.group("body")
        args: dict[str, Any] = {}
        for kv in _GEMMA_KV_RE.finditer(body):
            key = kv.group("key")
            if kv.group("str") is not None:
                args[key] = kv.group("str")
            elif kv.group("num") is not None:
                num = kv.group("num")
                args[key] = float(num) if "." in num else int(num)
            elif kv.group("bool") is not None:
                lit = kv.group("bool")
                args[key] = {"true": True, "false": False, "null": None}[lit]
        calls.append({"name": name, "args": args})
        parts.append(text[last_end : m.start()])
        last_end = m.end()
        matched = True
    if not matched:
        return [], text
    parts.append(text[last_end:])
    return calls, "".join(parts)


def _strip_mistral(text: str) -> tuple[list[_ParsedCall], str]:
    """Special-case Mistral's [TOOL_CALLS] marker.

    The body is one balanced JSON object/array starting at the first ``{`` or
    ``[`` after the marker. An optional ``[/TOOL_CALLS]`` close tag is
    consumed if present.
    """
    calls: list[_ParsedCall] = []
    parts: list[str] = []
    pos = 0
    while True:
        m = _MISTRAL_OPEN_RE.search(text, pos)
        if m is None:
            break
        body_start = m.end()
        balanced = _balanced_json_objects(text[body_start:])
        if not balanced:
            # Marker present but no parseable body; leave this occurrence alone.
            pos = m.end()
            continue
        # Take the first balanced object/array as the call body.
        body = balanced[0]
        parsed = _parse_body(body)
        if not parsed:
            pos = m.end()
            continue
        body_end = body_start + len(body)
        # Consume an optional [/TOOL_CALLS] close tag.
        close = _MISTRAL_CLOSE_RE.match(text, body_end)
        consume_end = close.end() if close else body_end
        calls.extend(parsed)
        parts.append(text[pos : m.start()])
        pos = consume_end
    if not calls:
        return [], text
    parts.append(text[pos:])
    return calls, "".join(parts)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ToolCallParserState(AgentState):
    """LangGraph state for the tool-call parser middleware."""


class ToolCallParserMiddleware(AgentMiddleware[ToolCallParserState, ContextT, ResponseT]):
    """Recover non-OpenAI-style tool calls emitted as text.

    Sits after the model call. When an `AIMessage` has empty `tool_calls`
    and non-empty content, this middleware tries to parse known text-based
    tool-call formats (Mistral `[TOOL_CALLS]{}`, Hermes `<tool_call>{}</tool_call>`,
    fenced JSON) and convert them into structured `tool_calls`.

    No-op for messages that already have `tool_calls` or no recoverable text.

    Args:
        formats: Which formats to try, in order. Defaults to all three.
        log_recoveries: When `True`, emit an info log every time a tool call
            is recovered (useful for diagnosing local-model behaviour).
        strip_thinking: When `True`, remove `<think>...</think>` envelopes
            from the message body before parsing. Helps reasoning-mode
            models (qwen3, deepseek-r1 distills) whose tool call sits
            after the thinking block.
        empty_args_feedback: When `True`, intercept tool calls whose `args`
            dict is empty and the tool has required arguments, and return
            a corrective `ToolMessage` listing the missing fields rather
            than letting the tool no-op. Defaults to `False` because in
            practice small instruct models (gemma4, llama3.1:8b) often
            interpret the schema-error reply as "user changed the task"
            and abandon the run rather than self-correcting. Enable
            when working with larger models that can interpret
            structured error feedback.
    """

    state_schema = ToolCallParserState

    def __init__(
        self,
        *,
        formats: tuple[str, ...] = ("mistral", "hermes", "fenced", "gemma"),
        log_recoveries: bool = True,
        strip_thinking: bool = True,
        empty_args_feedback: bool = False,
    ) -> None:
        self._formats = formats
        self._log_recoveries = log_recoveries
        self._strip_thinking = strip_thinking
        self._empty_args_feedback = empty_args_feedback

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def wrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Run the model call, then post-process the response."""
        response = call_next(request)
        return self._maybe_recover(response)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        call_next: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async variant of `wrap_model_call`."""
        response = await call_next(request)
        return self._maybe_recover(response)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """Sync tool gate: intercept empty-args calls with a schema hint."""
        feedback = self._empty_args_check(request)
        if feedback is not None:
            return feedback
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        """Async tool gate: intercept empty-args calls with a schema hint."""
        feedback = self._empty_args_check(request)
        if feedback is not None:
            return feedback
        return await handler(request)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _empty_args_check(self, request: ToolCallRequest) -> ToolMessage | None:
        """Return a corrective `ToolMessage` if the call has empty args and the tool requires some.

        Only fires when `empty_args_feedback` is enabled. Inspects the bound
        tool's schema to determine required field names and returns them in
        the error message so the model knows what to fill in on the next
        turn.
        """
        if not self._empty_args_feedback:
            return None
        tool = request.tool
        if tool is None:
            return None
        args = request.tool_call.get("args") if hasattr(request.tool_call, "get") else None
        # ToolCall may be a TypedDict-as-dict; both code paths land here.
        if not isinstance(args, dict):
            args = {}
        if args:
            return None
        # Inspect args_schema for required fields.
        required = _required_fields_of_tool(tool)
        if not required:
            return None
        names = ", ".join(required)
        msg = (
            f"Error: tool '{tool.name}' was called with empty arguments. "
            f"Required fields: {names}. Re-call with values for each "
            f"required field."
        )
        if self._log_recoveries:
            logger.info(
                "Empty-args feedback for tool %s; required fields: %s",
                tool.name,
                names,
            )
        tool_call_id = (
            request.tool_call.get("id") if hasattr(request.tool_call, "get") else None
        )
        return ToolMessage(
            content=msg,
            tool_call_id=tool_call_id or "unknown",
            status="error",
        )

    def _maybe_recover(self, response: ModelResponse) -> ModelResponse:
        """Inspect each AIMessage in the response and recover tool calls.

        Returns the response unchanged when there is nothing to recover.
        """
        new_messages = list(response.result)
        changed = False
        for i, msg in enumerate(new_messages):
            if not isinstance(msg, AIMessage):
                continue
            if msg.tool_calls:
                continue  # Already structured; leave alone.
            text = msg.content if isinstance(msg.content, str) else ""
            if not text:
                continue
            parsed, residual = parse_tool_calls_from_text(
                text,
                formats=self._formats,
                strip_thinking=self._strip_thinking,
            )
            if not parsed:
                continue

            tool_calls = [
                {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "name": call["name"],
                    "args": call["args"],
                    "type": "tool_call",
                }
                for call in parsed
            ]
            new_messages[i] = msg.model_copy(
                update={
                    "content": residual,
                    "tool_calls": tool_calls,
                },
            )
            changed = True
            if self._log_recoveries:
                names = ", ".join(c["name"] for c in parsed)
                logger.info(
                    "Recovered %d tool call(s) from text: %s",
                    len(parsed),
                    names,
                )

        if not changed:
            return response
        return ModelResponse(
            result=new_messages,
            structured_response=response.structured_response,
        )
