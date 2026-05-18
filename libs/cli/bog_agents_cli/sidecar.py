"""Sidecar — one-shot read-only Q&A subagent.

Used by the ``/sidecar`` slash command. The user asks a question; this
module builds a *fresh* agent invocation with a read-only tool set
(``read_file``, ``glob``, ``grep``, plus optionally ``web_search``) and
a one-time snapshot of the parent's conversation, runs it to a
single-answer fixed point, and returns the answer text.

Design constraints from REVIEW.md T-1:

* Parent thread is **not touched**. The sidecar gets a copy of the
  summary; nothing it does mutates parent state.
* Tool surface is **read-only**. No ``execute``, no ``edit_file``,
  no shell, no checkpointing, no skills, no subagents. A model that
  tries to do write-ish work just gets "tool not available".
* Output is a single string (the assistant's final message). The
  caller formats it as a quoted block in the parent transcript.

Pure-logic module — accepts the model + tool list as arguments so
tests can drive it without a live LLM. The CLI wiring lives in
:mod:`bog_agents_cli.sidecar_controller`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


SIDECAR_SYSTEM_PROMPT = """You are a "sidecar" assistant — a fresh, isolated
subagent invoked to answer ONE specific question without disturbing a
parent conversation that's in the middle of work.

Constraints:

* You can READ files (``read_file``), navigate the project (``glob``,
  ``grep``), and search the web (``web_search``) when available. You
  CANNOT edit, run shell commands, or change state in any way.
* The parent agent will receive your final answer as a quoted block
  inside their ongoing conversation. Be concise. Cite file paths and
  line numbers when relevant.
* If you need information you can't access (a write-only resource, a
  service you don't have credentials for), say so explicitly rather
  than guessing.
* When you have an answer, stop. Do not propose follow-up actions for
  the parent — the parent owns that decision.

If you receive a context summary from the parent, treat it as
background only — answer the question in front of you, not the
parent's broader task.
"""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SidecarResult:
    """Outcome of one sidecar invocation.

    Attributes:
        answer: The assistant's final text. Empty when the run errored.
        ok: True when the invocation completed without raising.
        error: Human-readable error message; empty on success.
        tool_calls_made: Names of every tool the sidecar invoked, in
            order. Useful for the CLI to surface "consulted README.md"
            in the quoted block.
        iterations: Total agent turns. 0 means the model answered
            without calling any tools (often the case for trivial Qs).
    """

    answer: str = ""
    ok: bool = True
    error: str = ""
    tool_calls_made: list[str] = field(default_factory=list)
    iterations: int = 0

    def quote_for_parent(self) -> str:
        """Render the answer as a quoted block to drop into the parent transcript."""
        if not self.ok:
            return f"> ⚠ /sidecar failed: {self.error}"
        body = self.answer.strip() or "(no answer returned)"
        quoted = "\n".join("> " + line if line else ">" for line in body.splitlines())
        tools_note = (
            f"\n>\n> _(consulted: {', '.join(self.tool_calls_made)})_"
            if self.tool_calls_made
            else ""
        )
        return f"> **Sidecar reply:**\n>\n{quoted}{tools_note}"


# ---------------------------------------------------------------------------
# Read-only tool builder
# ---------------------------------------------------------------------------


def build_readonly_tools(
    *,
    working_dir: Any | None = None,  # noqa: ANN401 — Path or str; explicit for late langchain typing
    web_search: bool = True,
) -> list[BaseTool]:
    """Construct the read-only tool list a sidecar gets.

    Args:
        working_dir: Project root used by ``read_file`` / ``glob`` /
            ``grep`` for path resolution. Defaults to the caller's cwd.
        web_search: When True, append the SDK's ``web_search`` tool
            (skip in air-gapped environments).

    Returns:
        A fresh list of tool instances. Each call returns new instances
        so tests can mutate one without bleeding into another.
    """
    from pathlib import Path

    from langchain_core.tools import StructuredTool

    root = Path(working_dir) if working_dir else Path.cwd()
    root = root.resolve()

    def _resolve_safe(rel: str) -> Path:
        """Resolve *rel* under *root*, refusing escapes via ``..`` or symlinks.

        Raises:
            PermissionError: When the resolved path escapes the project
                root or is itself a symlink.
        """
        candidate = (root / rel).resolve()
        # The sidecar must never read above the project root — REVIEW.md T-1
        # explicitly says the parent's state stays untouched, and reaching
        # into /etc or ~ falls under "untouched + safe".
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            msg = f"path {rel!r} resolves outside the working directory"
            raise PermissionError(msg) from exc
        if candidate.is_symlink():
            msg = f"refusing to follow symlink {rel!r}"
            raise PermissionError(msg)
        return candidate

    def read_file(path: str, *, max_bytes: int = 200_000) -> str:
        """Read a UTF-8 text file under the sidecar's working directory."""
        try:
            resolved = _resolve_safe(path)
        except PermissionError as exc:
            return f"Error: {exc}"
        if not resolved.is_file():
            return f"Error: {path!r} is not a regular file"
        try:
            data = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Error reading {path!r}: {exc}"
        if len(data) > max_bytes:
            return data[:max_bytes] + f"\n\n…[truncated at {max_bytes} bytes]…"
        return data

    def glob_files(pattern: str, *, limit: int = 200) -> str:
        """Match files under the working directory by glob pattern."""
        matches = [
            str(p.relative_to(root))
            for p in root.glob(pattern)
            if p.is_file() and not p.is_symlink()
        ]
        matches.sort()
        if len(matches) > limit:
            head = matches[:limit]
            return "\n".join(head) + f"\n…[{len(matches) - limit} more truncated]…"
        return "\n".join(matches) if matches else "(no matches)"

    def grep(
        pattern: str,
        *,
        path: str = ".",
        max_results: int = 100,
    ) -> str:
        """Search for *pattern* (regex) under *path*, returning file:line: matches."""
        import re

        try:
            resolved = _resolve_safe(path)
        except PermissionError as exc:
            return f"Error: {exc}"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Error: bad regex {pattern!r}: {exc}"
        targets: list[Path] = []
        if resolved.is_file():
            targets.append(resolved)
        elif resolved.is_dir():
            for p in resolved.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    targets.append(p)
        found: list[str] = []
        for f in targets:
            try:
                for n, line in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if regex.search(line):
                        found.append(f"{f.relative_to(root)}:{n}: {line.strip()}")
                        if len(found) >= max_results:
                            return "\n".join(found) + "\n…[result cap hit]…"
            except OSError:
                continue
        return "\n".join(found) if found else "(no matches)"

    tools: list[BaseTool] = [
        StructuredTool.from_function(read_file, name="read_file"),
        StructuredTool.from_function(glob_files, name="glob"),
        StructuredTool.from_function(grep, name="grep"),
    ]

    if web_search:
        tools.append(_build_web_search_tool())
    return tools


def _build_web_search_tool() -> BaseTool:
    """Return a minimal web_search tool.

    Offline-safe: returns an explanatory string when no provider is
    configured. Real providers (Tavily, etc.) plug in via environment
    variables.
    """
    import os

    from langchain_core.tools import StructuredTool

    def web_search(query: str, *, max_results: int = 5) -> str:
        """Search the public web for *query*. Returns ranked snippets."""
        # Try Tavily first — TAVILY_API_KEY is the canonical env var.
        if os.environ.get("TAVILY_API_KEY"):
            try:
                from langchain_tavily import (
                    TavilySearch,  # type: ignore[import-not-found]
                )
            except ImportError:
                return (
                    "web_search: TAVILY_API_KEY is set but ``langchain-tavily`` is not "
                    "installed. ``pip install langchain-tavily`` to enable."
                )
            try:
                tool = TavilySearch(max_results=max_results)
                return str(tool.invoke({"query": query}))
            except Exception as exc:
                return f"web_search: provider call failed: {exc}"
        return (
            "web_search: no provider configured. Set TAVILY_API_KEY (or another "
            "supported provider) to enable web search inside /sidecar."
        )

    return StructuredTool.from_function(web_search, name="web_search")


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_sidecar_query(
    *,
    question: str,
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    context_summary: str = "",
    system_prompt: str = SIDECAR_SYSTEM_PROMPT,
    max_iterations: int = 8,
) -> SidecarResult:
    """Run a one-shot sidecar invocation.

    The runner intentionally re-implements the model→tool-call loop
    here (rather than using ``create_agent``) so we stay independent of
    the full SDK middleware stack: every middleware in the parent's
    stack is bypassed, the sidecar's tool surface is exactly what's
    passed in, and no checkpointing / state-mutation happens.

    Args:
        question: The user's question.
        model: A bound LangChain ``BaseChatModel``. Tool-binding is
            handled inside.
        tools: Read-only tools the model may invoke.
        context_summary: One-time text from the parent the model can
            read as background. Will not be modified.
        system_prompt: Override the default sidecar persona.
        max_iterations: Hard cap on model→tool→model cycles.

    Returns:
        :class:`SidecarResult`.
    """
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    if not question.strip():
        return SidecarResult(
            ok=False, error="empty question — pass /sidecar <your question>"
        )

    user_block = question.strip()
    if context_summary.strip():
        user_block = (
            "Background context from the parent agent (read-only — do not act on it):\n\n"
            f"{context_summary.strip()}\n\n"
            f"Question: {question.strip()}"
        )

    messages: list[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_block),
    ]

    try:
        bound = model.bind_tools(tools) if tools else model
    except (NotImplementedError, AttributeError):
        # Stub models in tests may lack bind_tools; fall through to raw model.
        bound = model

    tools_by_name = {t.name: t for t in tools}
    result = SidecarResult()

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration
        try:
            response = bound.invoke(messages)
        except Exception as exc:
            result.ok = False
            result.error = f"model call failed: {exc}"
            return result

        if not isinstance(response, AIMessage):
            # Stub model returned something else; treat content as answer.
            result.answer = str(getattr(response, "content", response))
            return result

        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            result.answer = _coerce_text(response.content)
            return result

        for call in tool_calls:
            name = call.get("name", "")
            args = call.get("args", {}) or {}
            tool = tools_by_name.get(name)
            result.tool_calls_made.append(name)
            if tool is None:
                tool_text = (
                    f"Error: tool {name!r} is not available in /sidecar. "
                    "Sidecar tools are read-only."
                )
            else:
                try:
                    tool_text = str(tool.invoke(args))
                except Exception as exc:
                    tool_text = f"Error invoking {name}: {exc}"
            messages.append(
                ToolMessage(
                    content=tool_text,
                    tool_call_id=str(call.get("id", "")),
                    name=name,
                )
            )

    result.ok = False
    result.error = (
        f"max_iterations={max_iterations} hit before sidecar produced an answer"
    )
    return result


def _coerce_text(content: Any) -> str:  # noqa: ANN401
    """Reduce LangChain AIMessage.content (str or list of blocks) to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Public summary helper
# ---------------------------------------------------------------------------


def summarize_parent_context(
    messages: Sequence[Any],
    *,
    max_chars: int = 4000,
) -> str:
    """Build a one-shot text summary of the parent's recent conversation.

    Reads the **last few** turns of the parent's messages, truncating
    aggressively. Used by the CLI to populate ``context_summary`` for
    :func:`run_sidecar_query`. The sidecar agent is told to treat this
    as background only.

    Args:
        messages: The parent's message list (LangChain messages).
        max_chars: Hard ceiling on returned text length. The tail of the
            conversation is preferred since LLMs anchor on recent
            context.
    """
    if not messages:
        return ""
    lines: list[str] = []
    for msg in messages[-12:]:
        role = type(msg).__name__.replace("Message", "")
        text = _coerce_text(getattr(msg, "content", ""))
        if not text:
            continue
        lines.append(f"[{role}] {text.strip()}")
    blob = "\n\n".join(lines)
    if len(blob) > max_chars:
        # Keep the tail — it's the most recent context the user cares about.
        blob = "…[earlier turns truncated]…\n\n" + blob[-max_chars:]
    return blob
