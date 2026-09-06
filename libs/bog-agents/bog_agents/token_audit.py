"""Harness overhead you can attribute (ROADMAP #54).

Every agent turn starts with a fixed cost the user never typed: the assembled
system prompt, one JSON schema per tool, and whatever each middleware injects
on its way through the `wrap_model_call` chain. `audit_agent()` measures that
cost the only honest way — it builds the agent around a recording chat model,
runs one probe turn, and reports what the model actually received — and it
attributes the prompt and tool deltas to the middleware that made them by
instrumenting each instance's `wrap_model_call` / `awrap_model_call` before
the graph is compiled (`create_agent` hands the final middleware list, tools
and prompt to `notify_assembly` right before it calls LangChain).

Token counts use `tiktoken` (`o200k_base`) when the encoding is importable and
already cached locally, and a deterministic approximation otherwise; the
smoke-test baseline always uses the approximation so CI stays offline and
reproducible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# --------------------------------------------------------------------------- tokens

_APPROX_RE = re.compile(r"[A-Za-z]+|[0-9]{1,3}|[^\sA-Za-z0-9]|\s+")
_APPROX_LONG_WORD = 7
_ENCODER: Any = None
_ENCODER_TRIED = False
_ENCODER_LOCK = threading.Lock()


def _tiktoken_encoder() -> Any:  # noqa: ANN401 - optional dependency, typed as its own Encoding when present
    """Return the cached `o200k_base` encoder, or `None` when tiktoken is missing or would need the network."""
    global _ENCODER, _ENCODER_TRIED  # noqa: PLW0603 - process-wide cache
    with _ENCODER_LOCK:
        if _ENCODER_TRIED:
            return _ENCODER
        _ENCODER_TRIED = True
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001 - any failure (import, download, cache) means "approximate"
            _ENCODER = None
        return _ENCODER


def approx_tokens(text: str) -> int:
    """Deterministic, offline token estimate (words, numbers, punctuation, whitespace runs; long words count extra)."""
    if not text:
        return 0
    total = 0
    for match in _APPROX_RE.finditer(text):
        piece = match.group(0)
        if piece.isspace():
            if "\n" in piece:
                total += 1
            continue
        total += 1 + max(0, len(piece) - _APPROX_LONG_WORD) // _APPROX_LONG_WORD
    return total


def count_tokens(text: str, *, method: str = "auto") -> int:
    """Count tokens in `text`.

    Args:
        text: The text to measure.
        method: `"auto"` (tiktoken when available, else approximation),
            `"tiktoken"` (raise if unavailable) or `"approx"`.

    Returns:
        The token count.

    Raises:
        RuntimeError: When `method="tiktoken"` and the encoder cannot be loaded.
        ValueError: For an unknown method.
    """
    if method == "approx":
        return approx_tokens(text)
    encoder = _tiktoken_encoder()
    if method == "tiktoken":
        if encoder is None:
            msg = "tiktoken (o200k_base) is not available"
            raise RuntimeError(msg)
        return len(encoder.encode(text, disallowed_special=()))
    if method != "auto":
        msg = f"unknown token counting method {method!r}"
        raise ValueError(msg)
    if encoder is None:
        return approx_tokens(text)
    return len(encoder.encode(text, disallowed_special=()))


def tokenizer_label(method: str = "auto") -> str:
    """Human label for the counter `count_tokens` would use."""
    if method == "approx" or (method == "auto" and _tiktoken_encoder() is None):
        return "approx"
    return "o200k_base"


def message_text(message: BaseMessage | None) -> str:
    """Flatten a message's content (string or content blocks) to text."""
    if message is None:
        return ""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            parts.append(text if isinstance(text, str) else json.dumps(block, sort_keys=True))
        else:
            parts.append(str(block))
    return "\n".join(parts)


def tool_schema(tool: Any) -> dict[str, Any]:  # noqa: ANN401 - BaseTool | dict | callable
    """The OpenAI-style function schema the model is shown for `tool` (empty when it cannot be converted)."""
    try:
        return convert_to_openai_tool(tool)
    except Exception:  # noqa: BLE001 - odd tool shapes must not break an audit
        return {}


def tool_schema_tokens(tool: Any, *, method: str = "auto") -> int:  # noqa: ANN401 - BaseTool | dict | callable
    """Tokens of the serialized schema for one tool."""
    schema = tool_schema(tool)
    return count_tokens(json.dumps(schema, sort_keys=True), method=method) if schema else 0


# --------------------------------------------------------------------------- assembly hook


@dataclass
class AgentAssembly:
    """What `create_agent` is about to hand to LangChain."""

    middleware: list[Any]
    tools: list[Any]
    system_prompt: str | SystemMessage | None


_ASSEMBLY_HOOK: ContextVar[Callable[[AgentAssembly], None] | None] = ContextVar("bog_agents_assembly_hook", default=None)


def notify_assembly(middleware: Sequence[Any], tools: Sequence[Any] | None, system_prompt: str | SystemMessage | None) -> None:
    """Called by `create_agent` right before the graph is compiled; a no-op unless an audit is capturing."""
    hook = _ASSEMBLY_HOOK.get()
    if hook is not None:
        hook(AgentAssembly(list(middleware), list(tools or []), system_prompt))


@contextlib.contextmanager
def capture_assembly(hook: Callable[[AgentAssembly], None]) -> Iterator[None]:
    """Run `hook` for every `create_agent` assembled inside the block (same thread / task)."""
    token = _ASSEMBLY_HOOK.set(hook)
    try:
        yield
    finally:
        _ASSEMBLY_HOOK.reset(token)


# --------------------------------------------------------------------------- recording model


class RecordingChatModel(BaseChatModel):
    """A chat model that records every request it receives and answers with fixed text.

    `bind_tools` stores the tools and returns the model itself, so the recorded
    call carries exactly the schemas a real provider would have been sent.
    """

    reply: str = "ok"
    calls: list[dict[str, Any]] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "bog-recording"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:  # noqa: ANN401, ARG002 - mirrors BaseChatModel
        """Remember the tools and keep answering as this model."""
        self.bound_tools = list(tools)
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,  # noqa: ARG002 - BaseChatModel signature
        run_manager: Any = None,  # noqa: ANN401, ARG002 - BaseChatModel signature
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append({"messages": list(messages), "tools": list(self.bound_tools), "kwargs": dict(kwargs)})
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


# --------------------------------------------------------------------------- per-middleware attribution


@dataclass
class MiddlewareContribution:
    """Tokens one middleware added to (or removed from) the model request in the `wrap_model_call` chain."""

    name: str
    hook: str = "-"
    prompt_tokens: int = 0
    tool_tokens: int = 0
    message_tokens: int = 0
    observed: bool = False

    @property
    def total(self) -> int:
        """Net tokens attributed to this middleware."""
        return self.prompt_tokens + self.tool_tokens + self.message_tokens


def _request_stats(request: Any, method: str) -> tuple[int, int, int]:  # noqa: ANN401 - langchain ModelRequest
    system_message = getattr(request, "system_message", None)
    prompt = count_tokens(message_text(system_message), method=method) if system_message is not None else 0
    tools = sum(tool_schema_tokens(t, method=method) for t in (getattr(request, "tools", None) or []))
    messages = sum(count_tokens(message_text(m), method=method) for m in (getattr(request, "messages", None) or []))
    return prompt, tools, messages


def _instrument(middleware: Any, method: str) -> MiddlewareContribution:  # noqa: ANN401 - AgentMiddleware
    """Wrap `middleware`'s model-call hooks on the instance so the request delta it produces is recorded."""
    from langchain.agents.middleware.types import AgentMiddleware

    cls = type(middleware)
    entry = MiddlewareContribution(name=str(getattr(middleware, "name", cls.__name__)))

    def _record(before: tuple[int, int, int], request: Any) -> None:  # noqa: ANN401 - ModelRequest
        if entry.observed:
            return
        entry.observed = True
        after = _request_stats(request, method)
        entry.prompt_tokens = after[0] - before[0]
        entry.tool_tokens = after[1] - before[1]
        entry.message_tokens = after[2] - before[2]

    hooks: list[str] = []
    try:
        if cls.wrap_model_call is not AgentMiddleware.wrap_model_call:
            original = middleware.wrap_model_call

            def wrapped(request: Any, handler: Any) -> Any:  # noqa: ANN401 - langchain protocol
                before = _request_stats(request, method)

                def probe(inner: Any) -> Any:  # noqa: ANN401 - langchain protocol
                    _record(before, inner)
                    return handler(inner)

                return original(request, probe)

            middleware.wrap_model_call = wrapped
            hooks.append("wrap_model_call")
        if cls.awrap_model_call is not AgentMiddleware.awrap_model_call:
            original_async = middleware.awrap_model_call

            async def awrapped(request: Any, handler: Any) -> Any:  # noqa: ANN401 - langchain protocol
                before = _request_stats(request, method)

                async def probe(inner: Any) -> Any:  # noqa: ANN401 - langchain protocol
                    _record(before, inner)
                    return await handler(inner)

                return await original_async(request, probe)

            middleware.awrap_model_call = awrapped
            hooks.append("awrap_model_call")
    except (AttributeError, TypeError):
        entry.hook = "(not instrumentable)"
        return entry
    if hooks:
        entry.hook = "+".join(hooks)
    return entry


# --------------------------------------------------------------------------- the audit


@dataclass
class ToolCost:
    """Schema tokens for one tool as the model sees it."""

    name: str
    description_tokens: int
    schema_tokens: int


@dataclass
class TokenAudit:
    """One probe turn's fixed cost, attributed."""

    tokenizer: str
    assembled_prompt_tokens: int
    system_prompt_tokens: int
    tool_schema_tokens: int
    message_tokens: int
    probe_tokens: int
    tools: list[ToolCost] = field(default_factory=list)
    middleware: list[MiddlewareContribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def per_turn_overhead(self) -> int:
        """Tokens the harness adds to every turn before the user's own words."""
        return self.system_prompt_tokens + self.tool_schema_tokens + max(0, self.message_tokens - self.probe_tokens)

    @property
    def unattributed_prompt_tokens(self) -> int:
        """System-prompt tokens no instrumented hook accounts for (state-level injection, providers, or drift)."""
        return self.system_prompt_tokens - self.assembled_prompt_tokens - sum(m.prompt_tokens for m in self.middleware)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (what the CLI stores and the baseline test compares)."""
        return {
            "tokenizer": self.tokenizer,
            "per_turn_overhead": self.per_turn_overhead,
            "assembled_prompt_tokens": self.assembled_prompt_tokens,
            "system_prompt_tokens": self.system_prompt_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "message_tokens": self.message_tokens,
            "probe_tokens": self.probe_tokens,
            "unattributed_prompt_tokens": self.unattributed_prompt_tokens,
            "tools": [{"name": t.name, "description_tokens": t.description_tokens, "schema_tokens": t.schema_tokens} for t in self.tools],
            "middleware": [
                {
                    "name": m.name,
                    "hook": m.hook,
                    "prompt_tokens": m.prompt_tokens,
                    "tool_tokens": m.tool_tokens,
                    "message_tokens": m.message_tokens,
                    "observed": m.observed,
                }
                for m in self.middleware
            ],
            "notes": list(self.notes),
        }

    def render(self, *, top_tools: int = 12) -> str:
        """Plain-text report for a terminal."""
        injected = max(0, self.message_tokens - self.probe_tokens)
        lines = [
            f"Harness overhead: {self.per_turn_overhead:,} tokens per turn ({self.tokenizer})",
            f"  system prompt {self.system_prompt_tokens:,} | tool schemas {self.tool_schema_tokens:,} ({len(self.tools)} tools)"
            f" | injected messages {injected:,}",
            "",
            "Middleware (net change to the model request, outermost first):",
            f"  {'middleware':34s} {'prompt':>8s} {'tools':>8s} {'msgs':>8s}",
            f"  {'(assembled base prompt)':34s} {self.assembled_prompt_tokens:>8,} {'':>8s} {'':>8s}",
        ]
        for m in self.middleware:
            if m.hook == "-":
                continue
            flag = "" if m.observed else "  (not reached)"
            lines.append(f"  {m.name[:34]:34s} {m.prompt_tokens:>+8,} {m.tool_tokens:>+8,} {m.message_tokens:>+8,}{flag}")
        silent = [m.name for m in self.middleware if m.hook == "-"]
        if silent:
            lines.append(f"  no model-request hook ({len(silent)}): {', '.join(silent)}")
        lines.append(f"  {'(unattributed)':34s} {self.unattributed_prompt_tokens:>+8,}")
        lines.append("")
        lines.append(f"Tools by schema size (top {min(top_tools, len(self.tools))}):")
        lines.extend(
            f"  {t.name[:30]:30s} {t.schema_tokens:>7,}  (description {t.description_tokens:,})"
            for t in sorted(self.tools, key=lambda t: -t.schema_tokens)[:top_tools]
        )
        lines.extend(f"note: {n}" for n in self.notes)
        return "\n".join(lines)


def _tool_cost(tool: Any, method: str) -> ToolCost:  # noqa: ANN401 - BaseTool | dict | callable
    schema = tool_schema(tool)
    function = schema.get("function", {}) if isinstance(schema, dict) else {}
    return ToolCost(
        name=str(function.get("name") or getattr(tool, "name", "?")),
        description_tokens=count_tokens(str(function.get("description") or ""), method=method),
        schema_tokens=count_tokens(json.dumps(schema, sort_keys=True), method=method) if schema else 0,
    )


async def audit_agent_async(
    build: Callable[[BaseChatModel], Any],
    *,
    probe: str = "hi",
    method: str = "auto",
) -> TokenAudit:
    """Build an agent around a recording model with `build`, run one turn, and attribute its fixed cost.

    Args:
        build: Called with the recording model; returns the compiled graph (or a
            tuple whose first element is the graph, as the CLI's factory does).
        probe: The user message for the probe turn.
        method: Token counting method (see `count_tokens`).

    Returns:
        The audit.
    """
    model = RecordingChatModel()
    assemblies: list[AgentAssembly] = []
    contributions: list[MiddlewareContribution] = []

    def _hook(assembly: AgentAssembly) -> None:
        if assemblies:  # only the outermost agent (subagent graphs assemble too)
            return
        assemblies.append(assembly)
        contributions.extend(_instrument(mw, method) for mw in assembly.middleware)

    with capture_assembly(_hook):
        built = build(model)
    graph = built[0] if isinstance(built, tuple) else built
    await graph.ainvoke({"messages": [HumanMessage(content=probe)]})

    notes: list[str] = []
    if not model.calls:
        notes.append("the model was never called; the graph short-circuited before a model request")
        call: dict[str, Any] = {"messages": [], "tools": []}
    else:
        call = model.calls[0]
    system_messages = [m for m in call["messages"] if isinstance(m, SystemMessage)]
    other_messages = [m for m in call["messages"] if not isinstance(m, SystemMessage)]
    system_text = "\n".join(message_text(m) for m in system_messages)
    tools = [_tool_cost(tool, method) for tool in call["tools"]]
    assembled = assemblies[0].system_prompt if assemblies else None
    assembled_text = assembled if isinstance(assembled, str) else message_text(assembled)
    if not assemblies:
        notes.append("create_agent was not observed; per-middleware attribution is unavailable")
    return TokenAudit(
        tokenizer=tokenizer_label(method),
        assembled_prompt_tokens=count_tokens(assembled_text, method=method),
        system_prompt_tokens=count_tokens(system_text, method=method),
        tool_schema_tokens=sum(t.schema_tokens for t in tools),
        message_tokens=sum(count_tokens(message_text(m), method=method) for m in other_messages),
        probe_tokens=count_tokens(probe, method=method),
        tools=tools,
        middleware=contributions,
        notes=notes,
    )


def audit_agent(build: Callable[[BaseChatModel], Any], *, probe: str = "hi", method: str = "auto") -> TokenAudit:
    """Synchronous `audit_agent_async` (runs its own event loop, in a helper thread when one is already running)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(audit_agent_async(build, probe=probe, method=method))
    result: list[TokenAudit] = []
    errors: list[BaseException] = []

    def _runner() -> None:
        try:
            result.append(asyncio.run(audit_agent_async(build, probe=probe, method=method)))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=_runner, name="bog-token-audit", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def audit_create_agent(*, probe: str = "hi", method: str = "auto", **create_agent_kwargs: Any) -> TokenAudit:
    """Audit `bog_agents.create_agent(**create_agent_kwargs)` (the `model` is supplied by the audit)."""
    from bog_agents.graph import create_agent

    return audit_agent(lambda model: create_agent(model=model, **create_agent_kwargs), probe=probe, method=method)


__all__ = [
    "AgentAssembly",
    "MiddlewareContribution",
    "RecordingChatModel",
    "TokenAudit",
    "ToolCost",
    "approx_tokens",
    "audit_agent",
    "audit_agent_async",
    "audit_create_agent",
    "capture_assembly",
    "count_tokens",
    "message_text",
    "notify_assembly",
    "tokenizer_label",
    "tool_schema",
    "tool_schema_tokens",
]
